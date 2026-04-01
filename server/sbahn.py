"""
Model Train Synchronization Server

Connects to geops.io WebSocket API, tracks a real S3 train going toward
Mammendorf/Maisach, and drives the state machine that controls the model
train and station display.

Architecture:
    - ONE state machine on this server (train_state_machine.py)
    - Model train = dumb I/O (receives SPEED/STOP, sends HALL)
    - Station display = dumb I/O (receives STATION commands)

Usage:
    python sbahn.py
    Then type 'h' + Enter to simulate a HALL sensor trigger.
    Type 's' + Enter to see current status.
    Type 'q' + Enter to quit.
"""

import asyncio
import json
import sys
import time
import traceback
from datetime import datetime
import websockets

from train_state_machine import TrainStateMachine, State
from outputs import PrintModelOutput, PrintStationOutput
from tcp_model_output import TcpModelOutput, tcp_model_server
from tcp_station_output import TcpStationOutput, tcp_station_server

WS_URL = "wss://api.geops.io/realtime-ws/v1/?key=5cc87b12d7c5370001c1d655112ec5c21e0f441792cfc2fafe3e7a1e"

# Destinations we're looking for
TARGET_DESTINATIONS = ["Mammendorf", "Maisach", "München-Giesing"]
PING_TIMEOUT = 10  # seconds - if no PING received from geops.io, consider connection dead


def load_stations(path: str = "travel_times.json") -> list:
    """Load station list from travel_times.json."""
    with open(path) as f:
        data = json.load(f)
    return data["stations"]


# ── WebSocket helper functions ──────────────────────────────────────────

async def get_station_uic(ws, station_name: str) -> str | None:
    """Get UIC code for a station."""
    await ws.send("GET station")
    try:
        async with asyncio.timeout(5):
            async for msg in ws:
                data = json.loads(msg)
                if data.get("source") == "station":
                    content = data.get("content", {})
                    props = content.get("properties", {})
                    name = props.get("name", "")
                    uic = props.get("uic")
                    network = props.get("networkLines")
                    if station_name in name and network:
                        print(f"✅ Station: {name} → UIC: {uic}")
                        return uic
    except asyncio.TimeoutError:
        print("⏱️  Timeout getting station UIC")
    return None


async def get_incoming_trains(ws, uic: str, max_trains: int = 100) -> list:
    """Get timetable entries for a station."""
    await ws.send(f"GET timetable_{uic}")
    trains = []
    try:
        async with asyncio.timeout(5):
            async for msg in ws:
                data = json.loads(msg)
                if data.get("source", "").startswith("timetable_"):
                    content = data.get("content", {})
                    train_number = content.get("train_number")
                    destination = (content.get("to") or ["Unknown"])[0]
                    aimed_ms = content.get("aimedDepartureTime") or content.get("time", 0)
                    estimated_ms = content.get("departureTime") or aimed_ms
                    time_str = datetime.fromtimestamp(aimed_ms / 1000).strftime("%H:%M")
                    state = content.get("state")

                    # Filter out trains that are clearly not running (CANCELLED state if it exists)
                    if state == "CANCELLED":
                        continue

                    trains.append({
                        "number": train_number,
                        "destination": destination,
                        "time": time_str,
                        "timestamp": aimed_ms,        # scheduled (no delay) — used as base for live delay updates
                        "estimated_ms": estimated_ms, # current best estimate including known delay
                        "state": state,
                        "has_realtime": content.get("has_realtime_journey", False),
                    })
                    if len(trains) >= max_trains:
                        break
    except asyncio.TimeoutError:
        pass
    trains.sort(key=lambda t: t["timestamp"])
    return trains


def pick_target_train(trains: list, exclude_before_ms: float = 0) -> int | None:
    """Pick first train going to one of our target destinations in the next 30 minutes.
    
    Args:
        trains:           Timetable list from get_incoming_trains().
        exclude_before_ms: Skip trains whose scheduled timestamp is <= this value.
                          Pass the scheduled_ms of the last tracked train so stale
                          timetable entries for already-passed trains are ignored,
                          even if they still appear as 'upcoming' in the API.
    """
    import time
    now_ms = time.time() * 1000
    max_future_ms = now_ms + (30 * 60 * 1000)  # 30 minutes from now

    for t in trains:
        dest = t.get("destination", "")
        timestamp = t.get("timestamp", 0)
        number = t.get("number")

        # Skip trains at or before the last tracked train's scheduled slot
        if timestamp <= exclude_before_ms:
            continue

        # Only consider trains departing in the next 30 minutes
        if timestamp < now_ms or timestamp > max_future_ms:
            continue

        if any(d in dest for d in TARGET_DESTINATIONS):
            print(f"🎯 Selected: Train {number} → {dest} @ {t['time']}")
            return number
    return None


# ── Live tracking with state machine ───────────────────────────────────

async def keep_alive(ws):
    """Send a text PING message every 10 seconds — required by the geops.io API to keep the connection alive."""
    while True:
        try:
            await asyncio.sleep(PING_TIMEOUT)
            await ws.send("PING")
        except Exception:
            break


async def subscribe_bbox(ws):
    """Subscribe to live BBOX data (call once per WebSocket connection)."""
    await ws.send("BUFFER 100 100")
    await asyncio.sleep(0.1)
    await ws.send("BBOX 1269000 6087000 1350000 6200000 5 tenant=sbm")
    print("📡 Subscribed to BBOX live data\n")


async def track_with_state_machine(ws, train_number: int, sm: TrainStateMachine, scheduled_ms: float):
    """
    Feed live train data into the state machine until SM reaches WAITING_AT_NONAME.
    This covers the full cycle:
      - watching for the train to board/drive along the route
      - after Fasanenpark the model drives back to noname autonomously
      - we keep draining the WebSocket (required to prevent backpressure) until
        the HALL sensor fires and SM enters WAITING_AT_NONAME

    Returns True if the train completed a full cycle (departed at least once).
    Returns False if the cycle was aborted (restart requested or 90 s no-data timeout).
    """
    print(f"👁️  Watching for train {train_number}...\n")

    NO_DATA_TIMEOUT = 90  # seconds — if selected train silent this long, re-select

    # Only exit when the SM has left WAITING_AT_NONAME and then returned to it.
    # Without this guard the check would fire immediately on the first message
    # (since the SM starts in WAITING_AT_NONAME), burning through all trains.
    departed = False
    last_train_msg: float = time.time()  # wall-clock of last message FOR our train

    sm.restart_event.clear()

    async for message in ws:
        # Check for manual restart request from station display
        if sm.restart_event.is_set():
            print("🔄 Restart requested — aborting current tracking cycle")
            sm.restart_event.clear()
            return False

        # Check no-data timeout (only while waiting for the train to appear)
        if not departed and time.time() - last_train_msg > NO_DATA_TIMEOUT:
            print(f"⏱️  No data from train {train_number} for {NO_DATA_TIMEOUT}s — soft-restarting")
            return False

        try:
            data = json.loads(message)
            source = data.get("source", "")
            content = data.get("content")

            if source == "buffer":
                # Buffer contains a batch of updates
                for item in content or []:
                    if not item:
                        continue
                    trajectory = item.get("content")
                    if _is_our_train(trajectory, train_number):
                        last_train_msg = time.time()
                    _process_train_update(trajectory, train_number, sm, scheduled_ms)

            elif source.startswith("trajectory"):
                # Individual trajectory update
                if _is_our_train(content, train_number):
                    last_train_msg = time.time()
                _process_train_update(content, train_number, sm, scheduled_ms)

        except json.JSONDecodeError:
            pass
        except Exception as e:
            print(f"❌ Error processing update: {e}")
            traceback.print_exc()

        if sm.state != State.WAITING_AT_NONAME:
            departed = True
        elif departed:
            return True  # full cycle complete

    # WebSocket closed before the train completed its cycle — never advance
    # last_scheduled_ms. The main loop will re-select the same in-progress train
    # (it's designed for this: "pick_target_train will re-select it naturally").
    return False


def _is_our_train(data: dict, train_number: int) -> bool:
    """Return True if this trajectory update belongs to the tracked train."""
    if not isinstance(data, dict):
        return False
    return data.get("properties", {}).get("train_number") == train_number


def _process_train_update(
    data: dict, train_number: int, sm: TrainStateMachine,
    scheduled_ms: float
) -> None:
    """
    Process a single train update. Feed state changes to the state machine.
    """
    if not isinstance(data, dict):
        return

    props = data.get("properties", {})
    if props.get("train_number") != train_number:
        return

    new_state = props.get("state")
    raw_coords = props.get("raw_coordinates")

    # raw_coordinates is already [lon, lat] in EPSG:4326
    coordinates = None
    if raw_coords and len(raw_coords) >= 2:
        coordinates = [raw_coords[0], raw_coords[1]]  # [lon, lat]

    # Only feed state machine on actual state CHANGES (sm.last_api_state tracks current)
    if new_state and new_state != sm.last_api_state:
        now = datetime.now().strftime("%H:%M:%S")
        delay = props.get("delay") or 0
        delay_str = f" (delay: {delay/1000:.0f}s)" if delay else " (on time)"
        pos_str = f"\n   🗺️  https://www.google.com/maps?q={coordinates[1]},{coordinates[0]}" if coordinates else ""

        arrival_unix = int((scheduled_ms + delay) / 1000)

        icon = "🚉" if new_state == "BOARDING" else "🚆"
        print(f"\n[{now}] {icon} Real train: {new_state}{delay_str}{pos_str}")

        sm.on_api_state_change(new_state, coordinates, arrival_unix)


# ── Stdin listener for simulated HALL events ────────────────────────────

async def stdin_listener(sm: TrainStateMachine):
    """Listen for keyboard input to simulate HALL events and control."""
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)

    print("⌨️  Controls: [h] HALL sensor | [s] Status | [q] Quit\n")

    while True:
        line = await reader.readline()
        cmd = line.decode().strip().lower()

        if cmd == "h":
            now = datetime.now().strftime("%H:%M:%S")
            print(f"\n[{now}] 🧲 HALL sensor triggered!")
            sm.on_hall_sensor()
        elif cmd == "s":
            print(f"\n📊 {sm.status()}")
        elif cmd == "q":
            print("👋 Quitting...")
            raise KeyboardInterrupt
        elif cmd:
            print(f"   Unknown command '{cmd}'. Use: h=HALL, s=status, q=quit")


# ── Main ────────────────────────────────────────────────────────────────

async def main():
    """Main entry point: connect, find train, run state machine."""
    # Load station data
    stations = load_stations()
    print(f"📋 Loaded {len(stations)} stations from travel_times.json")
    for i, st in enumerate(stations):
        tt = st.get("travel_time_to_next")
        tt_str = f" → {tt}s" if tt else ""
        print(f"   {i}: {st['name']}{tt_str}")
    print()

    # Create output interfaces
    model = TcpModelOutput()
    station = TcpStationOutput()

    # Create state machine
    sm = TrainStateMachine(model, station, stations)
    print(f"🔧 State machine initialized: {sm.state.name}\n")

    # Start TCP servers
    tcp_server = await tcp_model_server(model, sm)
    await tcp_station_server(station, sm.restart_event)
    print()

    # These survive WebSocket reconnections
    stdin_task = asyncio.create_task(stdin_listener(sm))
    last_scheduled_ms: float = 0  # scheduled timestamp of the last tracked train
    # UIC for Fasanenpark is hardcoded in travel_times.json — read it directly
    # rather than calling GET station over the WebSocket, which races with live
    # BBOX messages and times out unreliably.
    fasanenpark = next((s for s in stations if s["name"] == "Fasanenpark"), None)
    if not fasanenpark or not fasanenpark.get("uic"):
        print("❌ Fasanenpark not found in travel_times.json")
        return
    uic: str = fasanenpark["uic"]
    print(f"📍 Fasanenpark UIC: {uic} (from travel_times.json)\n")

    try:
        while True:  # reconnection loop
            try:
                async with websockets.connect(WS_URL, max_size=10 * 1024 * 1024) as ws:
                    print("🔌 Connected to geops.io WebSocket\n")
                    keepalive_task = asyncio.create_task(keep_alive(ws))
                    try:
                        await subscribe_bbox(ws)

                        while True:  # main train loop
                            # On reconnect the SM may be mid-cycle.
                            #
                            # DRIVING_TO_NONAME: real train is gone, HALL sensor hasn't fired yet.
                            # The SM ignores all API events in this state, so there is nothing to
                            # track — just drain the WebSocket to avoid backpressure until HALL fires.
                            #
                            # Any other non-WAITING state: the same in-progress train is still in
                            # the timetable (last_scheduled_ms wasn't updated yet), so pick_target_train
                            # will re-select it naturally — no special handling needed.

                            # If the train departed long enough ago to have passed Fasanenpark but
                            # the SM wasn't updated before the outage, force it forward now.
                            _ROUTE_MS = (1440 + 300) * 1000  # 1440 s route + 5 min tolerance
                            if (sm.state not in (State.WAITING_AT_NONAME, State.DRIVING_TO_NONAME)
                                    and last_scheduled_ms > 0
                                    and time.time() * 1000 > last_scheduled_ms + _ROUTE_MS):
                                elapsed_min = (time.time() * 1000 - last_scheduled_ms) / 60000
                                print(f"🚨 Train departed {elapsed_min:.0f} min ago "
                                      f"— already past Fasanenpark. Forcing {sm.state.name} → DRIVING_TO_NONAME")
                                sm.force_driving_to_noname()

                            if sm.state == State.DRIVING_TO_NONAME:
                                print("⏳ Model returning to noname — draining WebSocket until HALL fires...")
                                _NONAME_TIMEOUT = 300  # seconds — max time to wait for HALL on return journey
                                _drain_start = time.time()
                                async for message in ws:
                                    if sm.state == State.WAITING_AT_NONAME:
                                        break
                                    if time.time() - _drain_start > _NONAME_TIMEOUT:
                                        print(f"⚠️  HALL not received after {_NONAME_TIMEOUT}s "
                                              f"— forcing WAITING_AT_NONAME to unblock")
                                        sm.force_waiting_at_noname()
                                        break
                                continue  # proceed to pick next train normally

                            trains = await get_incoming_trains(ws, uic)
                            if not trains:
                                print("❌ No trains found in timetable, retrying in 30s...")
                                await asyncio.sleep(30)
                                continue

                            print(f"\n📋 Timetable ({len(trains)} trains):")
                            for t in trains:
                                marker = "→" if any(d in t["destination"] for d in TARGET_DESTINATIONS) else " "
                                skip = " (already passed, skipping)" if t["timestamp"] <= last_scheduled_ms else ""
                                print(f"   {marker} {t['number']} → {t['destination']} @ {t['time']}{skip}")

                            train_number = pick_target_train(trains, exclude_before_ms=last_scheduled_ms)
                            if not train_number:
                                print(f"⏳ No suitable train in the next 30 minutes, retrying in 60s...")
                                await asyncio.sleep(60)
                                continue

                            print(f"\n{'='*60}")
                            print(f"  TRACKING TRAIN {train_number}")
                            print(f"  State machine: {sm.state.name}")
                            print(f"{'='*60}\n")

                            scheduled_ms = next(t["timestamp"] for t in trains if t["number"] == train_number)
                            estimated_ms = next(t["estimated_ms"] for t in trains if t["number"] == train_number)

                            # Send initial ETA immediately using departureTime (already includes known delay).
                            # Will be refined on every live API update as delay changes.
                            sm.eta_to_fasanenpark = int(estimated_ms / 1000)
                            sm.station.send_eta(sm.eta_to_fasanenpark)

                            train_departed = await track_with_state_machine(
                                ws, train_number, sm, scheduled_ms)
                            if train_departed:
                                # Normal completion (returned None via implicit return) or departed=True:
                                # the train ran its full cycle, advance the cursor.
                                last_scheduled_ms = scheduled_ms
                            else:
                                print(f"⚠️  Connection dropped before train {train_number} departed — "
                                      f"keeping last_scheduled_ms so it can be re-selected.")

                    finally:
                        keepalive_task.cancel()
                        try:
                            await keepalive_task
                        except asyncio.CancelledError:
                            pass

            except websockets.exceptions.ConnectionClosed as e:
                print(f"\n🔌 WebSocket connection closed ({e}). Reconnecting in 5s...")
                await asyncio.sleep(5)
            except OSError as e:
                print(f"\n🔌 Network error ({e}). Reconnecting in 10s...")
                await asyncio.sleep(10)

    except KeyboardInterrupt:
        print("\n👋 Stopped by user")
    finally:
        stdin_task.cancel()

    print(f"\n📊 Final: {sm.status()}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Bye!")
        sys.exit(0)
