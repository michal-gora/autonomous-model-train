"""
Virtual train fallback — used when the geops.io API is unavailable.

When the WebSocket connection has been down for longer than FALLBACK_TIMEOUT
seconds, the servers activate a virtual train that reads the static
fallback_timetable.json to simulate BOARDING/DRIVING events on a realistic
schedule.  Intermediate station boarding times are computed from the
``station_offsets_seconds`` table in fallback_timetable.json; the
``travel_time_to_next`` values in travel_times.json are used only as a
per-station fallback when an explicit offset is missing from the table.

As soon as a real WebSocket connection is re-established the caller cancels
the fallback task and live-data operation resumes.  The state machine's
GPS-based sync on the first real BOARDING corrects any drift that occurred
during the offline period.

Two public coroutines:
    virtual_train_for_state_machine()  — sbahn.py (state-machine mode)
    virtual_train_for_magnet_mode()    — magnet_station_server.py

Timetable format (fallback_timetable.json):
    Each service entry has an "arrival" (HH:MM) at the anchor station
    (Fasanenpark) and a "type" ("regular" or "rush_hour").
    Edit the JSON directly to adjust the schedule; no code changes needed.
"""

import asyncio
import json
from pathlib import Path
from datetime import datetime, timedelta, time as dt_time

_HERE = Path(__file__).parent
DEFAULT_TIMETABLE_PATH = str(_HERE / "fallback_timetable.json")

# ── Fallback configuration ──────────────────────────────────────────────

# Seconds of continuous API outage before activating the virtual train.
FALLBACK_TIMEOUT: float = 60.0

# How long the virtual train dwells at each station (simulates BOARDING).
VIRTUAL_STATION_DWELL: float = 8.0


# ── Timetable loading ───────────────────────────────────────────────────

def load_fallback_timetable(path: str = DEFAULT_TIMETABLE_PATH) -> tuple[list[dict], dict[str, int]]:
    """Load the timetable from *path*.

    Returns ``(services, offsets)`` where:
    - *services* is a sorted list of service dicts (keys: ``arrival`` HH:MM, ``type``).
    - *offsets* is ``{station_name: seconds_before_anchor}``; Fasanenpark → 0.

    Times 00:xx–03:xx are treated as post-midnight and sorted after 23:xx.
    Returns ``([], {})`` if the file cannot be loaded.
    """
    def _sort_key(svc: dict) -> int:
        h, m = map(int, svc["arrival"].split(":"))
        return ((h + 24) if h < 4 else h) * 60 + m

    try:
        with open(path) as f:
            data = json.load(f)
        services = sorted(data.get("services", []), key=_sort_key)
        raw_offsets = data.get("station_offsets_seconds", {})
        # Strip the metadata comment key if present
        offsets = {k: int(v) for k, v in raw_offsets.items() if not k.startswith("_")}
        return services, offsets
    except FileNotFoundError:
        print(f"⚠️  [Virtual] Timetable not found at {path!r} — using fixed 20-min interval")
        return [], {}
    except Exception as exc:
        print(f"⚠️  [Virtual] Failed to load timetable ({exc}) — using fixed 20-min interval")
        return [], {}


# ── Schedule helpers ────────────────────────────────────────────────────

def next_arrival_datetime(
    services: list[dict],
    now: datetime | None = None,
) -> datetime | None:
    """Return the next scheduled anchor-station arrival after *now*.

    Searches today first, then tomorrow (handles late-night gaps).
    Returns ``None`` if *services* is empty.
    """
    if not services:
        return None
    if now is None:
        now = datetime.now()
    today = now.date()
    for day_offset in range(2):
        d = today + timedelta(days=day_offset)
        candidates = [
            datetime.combine(d, dt_time(*map(int, svc["arrival"].split(":"))))
            for svc in services
        ]
        future = [c for c in candidates if c > now]
        if future:
            return min(future)
    return None


def compute_station_schedule(
    anchor_arrival: datetime,
    stations: list,
    offsets: dict[str, int],
) -> list[tuple[dict, datetime]]:
    """Return ``(station_dict, boarding_datetime)`` for every station.

    *offsets* maps station name → seconds before the anchor arrival
    (Fasanenpark = 0, all others positive).  Stations not present in
    *offsets* fall back to the cumulative ``travel_time_to_next`` chain
    from travel_times.json so that the function still works when offsets
    are only partially specified.
    """
    # Build cumulative fallback offsets from travel_time_to_next
    n = len(stations)
    fallback_secs = [0] * n
    for i in range(n - 2, -1, -1):
        tt = stations[i].get("travel_time_to_next") or 0
        fallback_secs[i] = fallback_secs[i + 1] + tt

    result = []
    for i, st in enumerate(stations):
        secs_before = offsets.get(st["name"], fallback_secs[i])
        result.append((st, anchor_arrival - timedelta(seconds=secs_before)))
    return result


# ── Internal helper ─────────────────────────────────────────────────────

async def _sleep_or_stop(seconds: float, stop_event: asyncio.Event) -> bool:
    """Sleep for *seconds* but return early (``True``) when stop_event fires."""
    try:
        await asyncio.wait_for(asyncio.shield(stop_event.wait()), timeout=max(seconds, 0))
        return True
    except asyncio.TimeoutError:
        return False


async def _wait_until(
    target: datetime,
    stop_event: asyncio.Event,
    label: str = "",
) -> bool:
    """Sleep until *target* wall-clock time.  Returns ``True`` if stop_event fired."""
    now = datetime.now()
    wait_s = (target - now).total_seconds()
    if wait_s > 60:
        now_str = now.strftime("%H:%M:%S")
        print(f"[{now_str}] 🤖 [Virtual] {label}waiting {wait_s/60:.1f} min until {target.strftime('%H:%M')}")
    if wait_s > 0:
        return await _sleep_or_stop(wait_s, stop_event)
    return False


# ── State-machine mode (sbahn.py) ───────────────────────────────────────

async def virtual_train_for_state_machine(
    sm,
    stations: list,
    stop_event: asyncio.Event,
    *,
    timetable_path: str = DEFAULT_TIMETABLE_PATH,
    station_dwell: float = VIRTUAL_STATION_DWELL,
) -> None:
    """Feed synthetic BOARDING / DRIVING events into a TrainStateMachine.

    Uses the schedule from *timetable_path* to pick the next Fasanenpark
    arrival time, then fires BOARDING + DRIVING at each station in sequence
    with timing derived from ``station_offsets_seconds`` in the timetable file
    (falling back to ``travel_time_to_next`` in travel_times.json for any
    station not listed there).  After the last station the
    state machine handles the DRIVING_TO_NONAME transition itself.

    Falls back to a fixed 20-minute interval when the timetable is unavailable.
    Stops cleanly when *stop_event* is set.
    """
    services, offsets = load_fallback_timetable(timetable_path)

    if sm.state.name != "WAITING_AT_NONAME":
        print("🤖 [Virtual] Resetting state machine to WAITING_AT_NONAME")
        sm.force_waiting_at_noname()

    print("🤖 [Virtual] Fallback activated — simulating virtual train")

    while not stop_event.is_set():
        # ── Handle end-of-cycle return trip ─────────────────────────────
        # After a full virtual cycle the last DRIVING event from Fasanenpark
        # puts the SM into DRIVING_TO_NONAME, which ignores all API events
        # until a HALL sensor fires.  In virtual mode no physical HALL ever
        # fires, so we simulate the return trip by waiting the configured
        # noname travel time and then forcing the SM back to WAITING_AT_NONAME.
        if sm.state.name == "DRIVING_TO_NONAME":
            noname_travel_s = getattr(sm, "NONAME_TRAVEL_SECONDS", 20.0)
            now_str = datetime.now().strftime("%H:%M:%S")
            print(f"[{now_str}] 🤖 [Virtual] Model returning to noname "
                  f"(waiting {noname_travel_s:.0f}s for return trip)")
            if await _sleep_or_stop(noname_travel_s, stop_event):
                break
            sm.force_waiting_at_noname()

        # ── Find next scheduled run ──────────────────────────────────────
        anchor = next_arrival_datetime(services)
        if anchor is None:
            # No timetable: depart ~30 s from now on a 20-min cycle
            anchor = datetime.now() + timedelta(seconds=30)

        schedule = compute_station_schedule(anchor, stations, offsets)

        # Skip stations whose boarding time is already comfortably in the past
        # (train started before fallback was activated; join mid-route instead).
        now = datetime.now()
        remaining = [(st, bt) for st, bt in schedule if bt > now - timedelta(seconds=station_dwell * 2)]
        if not remaining:
            # Whole train already past — sleep briefly before searching for next service
            await _sleep_or_stop(30, stop_event)
            continue

        first_station, first_boarding = remaining[0]
        now_str = datetime.now().strftime("%H:%M:%S")
        print(f"[{now_str}] 🤖 [Virtual] Next service: Fasanenpark @ "
              f"{anchor.strftime('%H:%M')} | first stop: "
              f"{first_station['name']} @ {first_boarding.strftime('%H:%M:%S')}")

        # ── Simulate each station in order ───────────────────────────────
        for st, boarding_time in remaining:
            if stop_event.is_set():
                break

            if await _wait_until(boarding_time, stop_event):
                break

            coords = [st["lon"], st["lat"]]
            eta = int(anchor.timestamp())
            now_str = datetime.now().strftime("%H:%M:%S")
            print(f"[{now_str}] 🤖 [Virtual] BOARDING at {st['name']} "
                  f"(sched. {boarding_time.strftime('%H:%M')})")
            sm.on_api_state_change("BOARDING", coords, eta)

            if await _sleep_or_stop(station_dwell, stop_event):
                break

            now_str = datetime.now().strftime("%H:%M:%S")
            print(f"[{now_str}] 🤖 [Virtual] DRIVING from {st['name']}")
            sm.on_api_state_change("DRIVING", coords, None)

    print("🤖 [Virtual] Fallback deactivated")


# ── Magnet-station mode (magnet_station_server.py) ──────────────────────

async def virtual_train_for_magnet_mode(
    station_to_magnet: dict,
    stations: list,
    target_ref: list,
    target_changed: asyncio.Event,
    station_out,
    stop_event: asyncio.Event,
    *,
    timetable_path: str = DEFAULT_TIMETABLE_PATH,
    station_dwell: float = VIRTUAL_STATION_DWELL,
) -> None:
    """Advance the model through magnet stations on a timetable-based schedule.

    On each BOARDING event: updates *target_ref[0]* and sets *target_changed*
    so model_positioner_loop reacts exactly as it would for a live train.
    Station timing is derived from *timetable_path* + travel_times.json.

    Falls back to a fixed 20-minute interval when the timetable is unavailable.
    Stops cleanly when *stop_event* is set.
    """
    services, offsets = load_fallback_timetable(timetable_path)

    print("🤖 [Virtual] Fallback activated — simulating virtual magnet train")

    while not stop_event.is_set():
        # ── Find next scheduled run ──────────────────────────────────────
        anchor = next_arrival_datetime(services)
        if anchor is None:
            anchor = datetime.now() + timedelta(seconds=30)

        schedule = compute_station_schedule(anchor, stations, offsets)

        # Keep only magnet stations; skip ones already in the past
        now = datetime.now()
        remaining = [
            (st, bt) for st, bt in schedule
            if st["name"] in station_to_magnet
            and bt > now - timedelta(seconds=station_dwell * 2)
        ]
        if not remaining:
            # Whole train already past — sleep briefly before searching for next service
            await _sleep_or_stop(30, stop_event)
            continue

        first_station, first_boarding = remaining[0]
        now_str = datetime.now().strftime("%H:%M:%S")
        print(f"[{now_str}] 🤖 [Virtual] Next service: Fasanenpark @ "
              f"{anchor.strftime('%H:%M')} | first stop: "
              f"{first_station['name']} @ {first_boarding.strftime('%H:%M:%S')}")

        # ── Advance through magnet stations ──────────────────────────────
        for st, boarding_time in remaining:
            if stop_event.is_set():
                break

            if await _wait_until(boarding_time, stop_event):
                break

            magnet = station_to_magnet[st["name"]]
            eta = int(anchor.timestamp())
            station_out.send_eta(eta)

            now_str = datetime.now().strftime("%H:%M:%S")
            print(f"[{now_str}] 🤖 [Virtual] → magnet {magnet} ({st['name']}) "
                  f"(sched. {boarding_time.strftime('%H:%M')})")
            target_ref[0] = magnet
            target_changed.set()

            if await _sleep_or_stop(station_dwell, stop_event):
                break

    print("🤖 [Virtual] Fallback deactivated")
