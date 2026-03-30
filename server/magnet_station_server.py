#!/usr/bin/env python3
"""
Magnet-Station Mode Server

Alternative operating mode where the model train moves between physical magnet
stations on the track, corresponding to real-world S-Bahn stations.

Layout:  Fasanenpark (home) ←──── Unterhaching ←── Taufkirchen ←── Furth ←── Deisenhofen
         magnet index:  4            3                2              1          0

The model train starts and waits at Fasanenpark (magnet 4).  When the tracked
real-world train boards at Deisenhofen, the model drives to magnet 0.  Each
subsequent boarding event advances the model to the matching magnet.

No state machine, no HALL feedback.  We simply:
  1. Reuse sbahn.py to select and track a real train.
  2. On each BOARDING event, map the real station to a magnet index.
  3. Send LOOPS:X + SPEED:constant so the model passes through the right
     number of magnets to reach the target.
  4. Display "Fasanenpark" statically on the station LCD and update the ETA.

Usage:
    python magnet_station_server.py
    Then type 's' + Enter for status, 'q' + Enter to quit.
"""

import asyncio
import json
import sys
import time
import traceback
from datetime import datetime
import websockets

from tcp_model_output import TcpModelOutput, MODEL_TCP_PORT, PING_TIMEOUT as MODEL_PING_TIMEOUT
from tcp_station_output import TcpStationOutput, STATION_TCP_PORT, PING_TIMEOUT as STATION_PING_TIMEOUT
from sbahn import (
    WS_URL,
    TARGET_DESTINATIONS,
    PING_TIMEOUT as WS_PING_TIMEOUT,
    load_stations,
    get_incoming_trains,
    pick_target_train,
    keep_alive,
    subscribe_bbox,
)

# ── Configuration ───────────────────────────────────────────────────────

# Stations in track order (Deisenhofen → Fasanenpark).
# Magnet 0 is nearest to the start of the route, magnet 4 = Fasanenpark (home).
MAGNET_STATIONS = ["Deisenhofen", "Furth", "Taufkirchen", "Unterhaching", "Fasanenpark"]

# Constant speed for moving between magnets (PWM fraction 0–1)
DRIVE_SPEED = 0.60

# Brake tuning — sent to the model on every connect.
# Magnet distances are short, so we want stronger braking than the MCU default (0.88).
BRAKE_DECEL = 3.0       # braking strength coefficient (MCU default: 0.88)
BRAKE_DEAD_ZONE = 0.13  # effective-zero threshold (keep at MCU default)

# Timeout (seconds) — if selected train sends nothing for this long, re-select
NO_DATA_TIMEOUT = 90

# ── Magnet-index helpers ────────────────────────────────────────────────


def build_station_to_magnet(stations: list) -> dict[str, int]:
    """Map station names to magnet indices using MAGNET_STATIONS ordering.

    Only stations in MAGNET_STATIONS get an index; others are ignored.
    """
    mapping: dict[str, int] = {}
    for magnet_idx, magnet_name in enumerate(MAGNET_STATIONS):
        # Match by station name prefix (handles "Furth" vs "Furth(b Deisenhofen)")
        for st in stations:
            if st["name"].startswith(magnet_name) or magnet_name.startswith(st["name"]):
                mapping[st["name"]] = magnet_idx
                break
    return mapping


def find_station_by_coords(stations: list, coords: list[float]) -> str | None:
    """Find the nearest station to [lon, lat] coordinates.

    Returns the station name, or None if nothing is close enough.
    """
    if not coords or len(coords) < 2:
        return None
    lon, lat = coords[0], coords[1]
    best_name = None
    best_dist = float("inf")
    for st in stations:
        dlat = st["lat"] - lat
        dlon = st["lon"] - lon
        dist = dlat * dlat + dlon * dlon
        if dist < best_dist:
            best_dist = dist
            best_name = st["name"]
    # Sanity: if nearest station is more than ~2 km away, ignore
    if best_dist > 0.001:  # roughly 1 km² threshold
        return None
    return best_name


# ── TCP server for model ───────────────────────────────────────────────

async def model_tcp_server(
    model: TcpModelOutput,
    restart_event: asyncio.Event,
    confirmed_ref: list[int],
    commanded_ref: list[int],
    hall_event: asyncio.Event,
):
    """TCP server for the model controller.  Handles HELLO/PING/HALL."""

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        print(f"📡 Model TCP connection from {peer}")
        last_ping = asyncio.get_running_loop().time()

        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            hello = line.decode().strip()
            if hello != "HELLO:MODEL":
                print(f"❌ Expected HELLO:MODEL, got: {hello!r}")
                writer.close()
                return
            writer.write(b"ACK\n")
            await writer.drain()
            model.set_writer(writer)

            # Send brake parameters immediately so they override the MCU defaults.
            model.send_brake_decel(BRAKE_DECEL)
            model.send_brake_dead_zone(BRAKE_DEAD_ZONE)
            print(f"📤 → Model: BRAKE_DECEL:{BRAKE_DECEL}, BRAKE_DEAD_ZONE:{BRAKE_DEAD_ZONE}")

            # If the model was moving when it disconnected, re-send the command.
            if commanded_ref[0] != confirmed_ref[0]:
                delta = (commanded_ref[0] - confirmed_ref[0]) % len(MAGNET_STATIONS)
                loops = delta - 1
                print(f"🚀 Model reconnected; resuming move to magnet {commanded_ref[0]} "
                      f"({MAGNET_STATIONS[commanded_ref[0]]}): LOOPS:{loops}, SPEED:{DRIVE_SPEED}")
                model.send_loops(loops)
                model.send_speed(DRIVE_SPEED)
            else:
                print(f"🏠 Model connected at magnet {confirmed_ref[0]} "
                      f"({MAGNET_STATIONS[confirmed_ref[0]]}) — stopped")

            while True:
                try:
                    now = asyncio.get_running_loop().time()
                    if now - last_ping > MODEL_PING_TIMEOUT:
                        print(f"⚠️  No PING from model for {MODEL_PING_TIMEOUT}s — closing")
                        break
                    line = await asyncio.wait_for(reader.readline(), timeout=1.0)
                    if not line:
                        break
                    msg = line.decode().strip()
                    if msg == "PING":
                        last_ping = asyncio.get_running_loop().time()
                        writer.write(b"PONG\n")
                        await writer.drain()
                    elif msg == "HALL":
                        now_str = datetime.now().strftime("%H:%M:%S")
                        print(f"[{now_str}] 🧲 HALL received from model")
                        hall_event.set()
                    elif msg:
                        print(f"⚠️  Unknown message from model: {msg!r}")
                except asyncio.TimeoutError:
                    continue
        except asyncio.TimeoutError:
            print("❌ Model timed out during handshake")
        except Exception as e:
            print(f"❌ Model TCP error: {e}")
        finally:
            model.disconnect()
            try:
                writer.close()
            except Exception:
                pass

    server = await asyncio.start_server(handle_client, "0.0.0.0", MODEL_TCP_PORT)
    print(f"🌐 Model TCP server on 0.0.0.0:{MODEL_TCP_PORT}")
    return server


# ── TCP server for station display (simplified) ────────────────────────

async def station_tcp_server(station: TcpStationOutput, restart_event: asyncio.Event):
    """TCP server for the station display.  Handles HELLO/PING/RESTART."""

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        print(f"📡 Station TCP connection from {peer}")
        last_ping = asyncio.get_running_loop().time()

        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            hello = line.decode().strip()
            if hello != "HELLO:STATION":
                print(f"❌ Expected HELLO:STATION, got: {hello!r}")
                writer.close()
                return
            writer.write(b"ACK\n")
            await writer.drain()
            station.set_writer(writer)

            while True:
                try:
                    now = asyncio.get_running_loop().time()
                    if now - last_ping > STATION_PING_TIMEOUT:
                        print(f"⚠️  No PING from station for {STATION_PING_TIMEOUT}s — closing")
                        break
                    line = await asyncio.wait_for(reader.readline(), timeout=1.0)
                    if not line:
                        break
                    msg = line.decode().strip()
                    if msg == "PING":
                        last_ping = asyncio.get_running_loop().time()
                        writer.write(b"PONG\n")
                        await writer.drain()
                    elif msg == "RESTART":
                        print("🔄 RESTART received from station display")
                        restart_event.set()
                    elif msg:
                        print(f"⚠️  Unknown message from station: {msg!r}")
                except asyncio.TimeoutError:
                    continue
        except asyncio.TimeoutError:
            print("❌ Station timed out during handshake")
        except Exception as e:
            print(f"❌ Station TCP error: {e}")
        finally:
            station.disconnect()
            try:
                writer.close()
            except Exception:
                pass

    server = await asyncio.start_server(handle_client, "0.0.0.0", STATION_TCP_PORT)
    print(f"🌐 Station TCP server on 0.0.0.0:{STATION_TCP_PORT}")
    return server


# ── Train tracker (API only — no model interaction) ───────────────────────

async def _track_one_train(
    ws,
    train_number: int,
    scheduled_ms: float,
    stations: list,
    station_to_magnet: dict[str, int],
    station_out: TcpStationOutput,
    restart_event: asyncio.Event,
    target_ref: list[int],
    target_changed: asyncio.Event,
) -> bool:
    """
    Watch one train until it boards at Fasanenpark (return True) or is
    aborted (return False).  Updates target_ref[0] on every BOARDING event
    and sets target_changed so the model positioner can react.
    Never touches the model train.
    """
    home_magnet = len(MAGNET_STATIONS) - 1
    last_train_msg = time.time()
    last_api_state: str | None = None
    departed = False

    async for message in ws:
        if restart_event.is_set():
            restart_event.clear()
            print("🔄 [Tracker] Restart")
            return False

        if not departed and time.time() - last_train_msg > NO_DATA_TIMEOUT:
            print(f"⏱️  [Tracker] No data from train {train_number} for {NO_DATA_TIMEOUT}s")
            return False

        try:
            data = json.loads(message)
            source = data.get("source", "")
            content = data.get("content")

            items = []
            if source == "buffer":
                items = content or []
            elif source.startswith("trajectory"):
                items = [data]

            for item in items:
                trajectory = item.get("content") if source == "buffer" else item.get("content")
                if not isinstance(trajectory, dict):
                    continue

                props = trajectory.get("properties", {})
                if props.get("train_number") != train_number:
                    continue

                last_train_msg = time.time()
                new_state = props.get("state")
                if not new_state or new_state == last_api_state:
                    continue
                last_api_state = new_state

                raw_coords = props.get("raw_coordinates")
                coordinates = None
                if raw_coords and len(raw_coords) >= 2:
                    coordinates = [raw_coords[0], raw_coords[1]]

                delay = props.get("delay") or 0
                delay_str = f" (delay: {delay / 1000:.0f}s)" if delay else " (on time)"
                now_str = datetime.now().strftime("%H:%M:%S")
                icon = "🚉" if new_state == "BOARDING" else "🚆"
                print(f"\n[{now_str}] {icon} [Tracker] Train {train_number}: {new_state}{delay_str}")

                arrival_unix = int((scheduled_ms + delay) / 1000)
                station_out.send_eta(arrival_unix)

                if new_state == "BOARDING":
                    departed = True

                    nearest = find_station_by_coords(stations, coordinates)
                    if nearest and nearest in station_to_magnet:
                        real_magnet = station_to_magnet[nearest]
                    elif nearest:
                        print(f"   📍 Train at {nearest} — not in magnet range, ignoring")
                        continue
                    else:
                        print(f"   📍 Could not determine station from coords {coordinates}")
                        continue

                    print(f"   📍 Boarding at: {nearest} (magnet {real_magnet})")

                    if target_ref[0] != real_magnet:
                        target_ref[0] = real_magnet
                        target_changed.set()
                        print(f"   🎯 Target → magnet {real_magnet} ({MAGNET_STATIONS[real_magnet]})")
                    else:
                        print(f"   🎯 Target unchanged (magnet {real_magnet})")

                    if real_magnet == home_magnet:
                        print(f"   🏁 Train at Fasanenpark — cycle complete, searching next train")
                        return True

                elif new_state == "DRIVING" and departed:
                    if coordinates:
                        nearest = find_station_by_coords(stations, coordinates)
                        if nearest:
                            print(f"   📍 Departed from: {nearest}")

        except json.JSONDecodeError:
            pass
        except Exception as e:
            print(f"❌ [Tracker] Error: {e}")
            traceback.print_exc()

    return False


async def train_tracker_loop(
    stations: list,
    station_to_magnet: dict[str, int],
    station_out: TcpStationOutput,
    restart_event: asyncio.Event,
    target_ref: list[int],
    target_changed: asyncio.Event,
):
    """
    Continuously selects and tracks real S-Bahn trains.  On each BOARDING
    event, updates target_ref[0] and sets target_changed.  Completely
    independent of the model train.
    """
    fasanenpark = next(s for s in stations if s["name"] == "Fasanenpark")
    uic: str = fasanenpark["uic"]
    print(f"📍 [Tracker] Fasanenpark UIC: {uic}")
    last_scheduled_ms: float = 0

    while True:  # reconnection loop
        try:
            async with websockets.connect(WS_URL, max_size=10 * 1024 * 1024) as ws:
                print("🔌 [Tracker] Connected to geops.io WebSocket")
                keepalive = asyncio.create_task(keep_alive(ws))
                try:
                    await subscribe_bbox(ws)

                    while True:  # train selection loop
                        trains = await get_incoming_trains(ws, uic)
                        if not trains:
                            print("❌ [Tracker] No trains found, retrying in 30s...")
                            await asyncio.sleep(30)
                            continue

                        print(f"\n📋 Timetable ({len(trains)} trains):")
                        for t in trains:
                            marker = "→" if any(d in t["destination"] for d in TARGET_DESTINATIONS) else " "
                            skip = " (skipping)" if t["timestamp"] <= last_scheduled_ms else ""
                            print(f"   {marker} {t['number']} → {t['destination']} @ {t['time']}{skip}")

                        train_number = pick_target_train(trains, exclude_before_ms=last_scheduled_ms)
                        if not train_number:
                            print("⏳ [Tracker] No suitable train in next 30 min, retrying in 60s...")
                            await asyncio.sleep(60)
                            continue

                        scheduled_ms = next(t["timestamp"] for t in trains if t["number"] == train_number)
                        estimated_ms = next(t["estimated_ms"] for t in trains if t["number"] == train_number)

                        print(f"\n{'='*60}")
                        print(f"  [Tracker] TRACKING TRAIN {train_number}")
                        print(f"  Target currently: magnet {target_ref[0]} ({MAGNET_STATIONS[target_ref[0]]})")
                        print(f"{'='*60}\n")

                        station_out.send_eta(int(estimated_ms / 1000))

                        completed = await _track_one_train(
                            ws, train_number, scheduled_ms, stations,
                            station_to_magnet, station_out, restart_event,
                            target_ref, target_changed,
                        )

                        if completed:
                            last_scheduled_ms = scheduled_ms
                            print(f"\n✅ [Tracker] Train {train_number} done — searching next")
                        else:
                            print(f"⚠️  [Tracker] Aborted train {train_number} — will retry")

                finally:
                    keepalive.cancel()
                    try:
                        await keepalive
                    except asyncio.CancelledError:
                        pass

        except websockets.exceptions.ConnectionClosed as e:
            print(f"\n🔌 [Tracker] WS closed ({e}). Reconnecting in 5s...")
            await asyncio.sleep(5)
        except websockets.exceptions.InvalidStatus as e:
            print(f"\n🔌 [Tracker] WS rejected ({e}). Reconnecting in 10s...")
            await asyncio.sleep(10)
        except OSError as e:
            print(f"\n🔌 [Tracker] Network error ({e}). Reconnecting in 10s...")
            await asyncio.sleep(10)


# ── Model positioner (HALL only — no API interaction) ──────────────────

async def model_positioner_loop(
    model: TcpModelOutput,
    hall_event: asyncio.Event,
    target_ref: list[int],
    target_changed: asyncio.Event,
    confirmed_ref: list[int],
    commanded_ref: list[int],
):
    """
    Keeps the model train positioned at target_ref[0].

    Rules:
    - When target changes AND model is stopped: send move command immediately.
    - Once moving, do nothing until HALL confirms arrival.
    - On HALL: update confirmed position. If target still differs, send move.
    Never touches the WebSocket or train API.
    """
    n = len(MAGNET_STATIONS)

    def try_move():
        target = target_ref[0]
        confirmed = confirmed_ref[0]
        delta = (target - confirmed) % n
        if delta == 0:
            now_str = datetime.now().strftime("%H:%M:%S")
            print(f"[{now_str}] ✅ [Model] Already at target magnet {confirmed} ({MAGNET_STATIONS[confirmed]})")
            return
        loops = delta - 1
        model.send_loops(loops)
        model.send_speed(DRIVE_SPEED)
        commanded_ref[0] = target
        now_str = datetime.now().strftime("%H:%M:%S")
        print(f"[{now_str}] 🚀 [Model] Moving: magnet {confirmed} ({MAGNET_STATIONS[confirmed]})"
              f" → {target} ({MAGNET_STATIONS[target]}), LOOPS:{loops}, SPEED:{DRIVE_SPEED}")

    while True:
        is_moving = commanded_ref[0] != confirmed_ref[0]

        hall_task = asyncio.create_task(hall_event.wait())
        target_task = asyncio.create_task(target_changed.wait())

        await asyncio.wait({hall_task, target_task}, return_when=asyncio.FIRST_COMPLETED)

        hall_fired = hall_event.is_set()
        target_fired = target_changed.is_set()

        hall_task.cancel()
        target_task.cancel()
        await asyncio.gather(hall_task, target_task, return_exceptions=True)

        if hall_fired:
            hall_event.clear()
            target_changed.clear()  # consume any pending update; try_move reads fresh target
            confirmed_ref[0] = commanded_ref[0]
            now_str = datetime.now().strftime("%H:%M:%S")
            print(f"\n[{now_str}] 🧲 [Model] HALL confirmed: magnet {confirmed_ref[0]}"
                  f" ({MAGNET_STATIONS[confirmed_ref[0]]})")
            try_move()
        elif target_fired:
            target_changed.clear()
            if not is_moving:
                try_move()
            else:
                target = target_ref[0]
                now_str = datetime.now().strftime("%H:%M:%S")
                print(f"[{now_str}] ⏳ [Model] Target → magnet {target}"
                      f" ({MAGNET_STATIONS[target]}) — queued until HALL")


# ── Stdin listener ──────────────────────────────────────────────────────

async def stdin_listener(restart_event: asyncio.Event, status_fn):
    """Listen for keyboard input."""
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)

    print("⌨️  Controls: [s] Status | [q] Quit\n")

    while True:
        line = await reader.readline()
        cmd = line.decode().strip().lower()
        if cmd == "s":
            print(f"\n📊 {status_fn()}")
        elif cmd == "q":
            print("👋 Quitting...")
            raise KeyboardInterrupt
        elif cmd:
            print(f"   Unknown command '{cmd}'. Use: s=status, q=quit")


# ── Main ────────────────────────────────────────────────────────────────

async def main():
    """Main entry point for magnet-station mode."""
    stations = load_stations()
    print(f"📋 Loaded {len(stations)} stations from travel_times.json")

    station_to_magnet = build_station_to_magnet(stations)
    print(f"\n🧲 Magnet-station mapping:")
    for name, idx in sorted(station_to_magnet.items(), key=lambda x: x[1]):
        print(f"   magnet {idx}: {name}")
    print()

    if len(station_to_magnet) != len(MAGNET_STATIONS):
        missing = set(MAGNET_STATIONS) - {m for m in MAGNET_STATIONS if any(
            name.startswith(m) or m.startswith(name) for name in station_to_magnet)}
        print(f"⚠️  Warning: could not map all magnet stations. Missing: {missing}")

    model = TcpModelOutput()
    station_out = TcpStationOutput()
    restart_event = asyncio.Event()
    hall_event = asyncio.Event()
    target_changed = asyncio.Event()

    home_magnet = len(MAGNET_STATIONS) - 1
    # Shared position state — mutable refs so closures see updates
    target_ref    = [home_magnet]  # real train's current station (set by tracker)
    confirmed_ref = [home_magnet]  # model's confirmed position   (set on HALL)
    commanded_ref = [home_magnet]  # last commanded position       (set when move sent)

    await model_tcp_server(model, restart_event, confirmed_ref, commanded_ref, hall_event)
    await station_tcp_server(station_out, restart_event)
    print()

    station_out.send_station("Fasanenpark", "AT_STATION_VALID")

    def status_fn():
        moving_str = (f"moving → magnet {commanded_ref[0]} ({MAGNET_STATIONS[commanded_ref[0]]})"
                      if commanded_ref[0] != confirmed_ref[0] else "stopped")
        return (f"Target: magnet {target_ref[0]} ({MAGNET_STATIONS[target_ref[0]]}), "
                f"Confirmed: magnet {confirmed_ref[0]} ({MAGNET_STATIONS[confirmed_ref[0]]}), "
                f"{moving_str}")

    stdin_task = asyncio.create_task(stdin_listener(restart_event, status_fn))
    tracker_task = asyncio.create_task(train_tracker_loop(
        stations, station_to_magnet, station_out, restart_event,
        target_ref, target_changed,
    ))
    positioner_task = asyncio.create_task(model_positioner_loop(
        model, hall_event, target_ref, target_changed, confirmed_ref, commanded_ref,
    ))

    try:
        await asyncio.gather(stdin_task, tracker_task, positioner_task)
    except KeyboardInterrupt:
        print("\n👋 Stopped by user")
    finally:
        for task in (stdin_task, tracker_task, positioner_task):
            task.cancel()
        await asyncio.gather(stdin_task, tracker_task, positioner_task, return_exceptions=True)

    moving_str = (f"moving → {commanded_ref[0]}" if commanded_ref[0] != confirmed_ref[0] else "stopped")
    print(f"\n📊 Final: target={target_ref[0]}, confirmed={confirmed_ref[0]}, {moving_str}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Bye!")
        sys.exit(0)
