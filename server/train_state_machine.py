"""
6-State Moore State Machine for Model Train Synchronization.

All decision logic lives here. The server feeds inputs (API events, HALL sensor)
and reads outputs (commands for model train and station display).

States:
    WAITING_AT_NONAME    - Model parked at noname, waiting for real train to board
    AT_STATION_VALID     - Model at station, real train is boarding (validity ON)
    AT_STATION_WAITING   - Model at station, real train still driving (validity OFF)
    DRIVING              - Model driving between stations
    DRIVING_TO_NONAME    - Model driving from Fasanenpark back to noname
    RUNNING_TO_STATION   - Model catching up at full speed (it's late)
"""

import json
import math
import asyncio
from enum import Enum, auto
from datetime import datetime


class State(Enum):
    WAITING_AT_NONAME = auto()
    AT_STATION_VALID = auto()
    AT_STATION_WAITING = auto()
    DRIVING = auto()
    DRIVING_TO_NONAME = auto()
    RUNNING_TO_STATION = auto()


class TrainStateMachine:
    """ONE state machine that controls both model train and station display."""

    TRACK_LOOP_SECONDS = 20.0   # Time for model to go one station at full speed (calibrate!)
    NONAME_TRAVEL_SECONDS = 20.0  # Time from Fasanenpark to noname (calibrate!)
    MIN_SPEED = 0.5
    MAX_SPEED = 1.0
    def __init__(self, model_output, station_output, stations: list):
        """
        Args:
            model_output:   Object with send_speed(float), send_stop(), send_loops(int) methods
            station_output: Object with send_station(name, valid) and send_clear() methods
            stations:       List of station dicts from travel_times.json
        """
        self.model = model_output
        self.station = station_output
        self.stations = stations

        # State variables
        self.state: State = State.WAITING_AT_NONAME
        self.current_station_index: int | None = None
        self.last_api_state: str | None = None  # "BOARDING" or "DRIVING"
        self.travel_speed: float = 0.0
        self.current_loops: int = 0           # last value sent via send_loops(); replayed on MCU reconnect
        self.eta_to_fasanenpark: int | None = None  # absolute unix timestamp of expected arrival at Fasanenpark

        # Ensure the train is stopped at startup, then apply initial outputs.
        self.model.send_stop()
        self._apply_outputs()

        # Event set by the station display (RESTART button) or the tracking loop
        # (90 s no-data timeout) to abort the current tracking cycle and re-select a train.
        self.restart_event: asyncio.Event = asyncio.Event()

    # ── Public API: feed events ─────────────────────────────────────────

    def on_api_state_change(self, new_api_state: str, coordinates: list | None = None, arrival_unix: int | None = None):
        """Called when the real train changes state (BOARDING / DRIVING).

        Args:
            new_api_state:  "BOARDING" or "DRIVING"
            coordinates:    [lon, lat] from geops.io (EPSG:4326 already converted)
            arrival_unix:   Expected arrival time at Fasanenpark as unix timestamp (scheduled + delay)
        """
        old_state = self.state
        self.last_api_state = new_api_state

        if arrival_unix is not None:
            self.eta_to_fasanenpark = arrival_unix
            self.station.send_eta(arrival_unix)

        new_state = self._transition_on_api(new_api_state, coordinates)
        if new_state and new_state != old_state:
            self._enter_state(new_state, from_state=old_state, coordinates=coordinates)

    def on_hall_sensor(self):
        """Called when the model train's hall sensor triggers (arrived at a station)."""
        old_state = self.state
        new_state = self._transition_on_hall()
        if new_state and new_state != old_state:
            self._enter_state(new_state, from_state=old_state)

    def force_driving_to_noname(self):
        """Force transition to DRIVING_TO_NONAME.

        Use when the real train passed Fasanenpark during an API outage so that
        normal API events can no longer trigger the transition. The model will
        drive back to noname and wait for the HALL sensor as usual.
        """
        if self.state != State.DRIVING_TO_NONAME:
            self._enter_state(State.DRIVING_TO_NONAME, from_state=self.state)

    def force_waiting_at_noname(self):
        """Force transition to WAITING_AT_NONAME.

        Use as an escape hatch when the model is stuck in DRIVING_TO_NONAME
        indefinitely (e.g. model disconnected and HALL never fires). Resets all
        tracking state so the next train can be picked normally.
        """
        if self.state != State.WAITING_AT_NONAME:
            self._enter_state(State.WAITING_AT_NONAME, from_state=self.state)

    # ── Transition logic ────────────────────────────────────────────────

    def _transition_on_api(self, api_state: str, coordinates: list | None) -> State | None:
        """Determine next state based on API event. Returns None if no transition."""
        s = self.state

        if s == State.WAITING_AT_NONAME:
            if api_state == "BOARDING":
                return State.AT_STATION_VALID
            elif api_state == "DRIVING":
                return State.DRIVING

        elif s == State.AT_STATION_VALID:
            if api_state == "DRIVING":
                if self._is_fasanenpark():
                    return State.DRIVING_TO_NONAME
                else:
                    return State.DRIVING

        elif s == State.DRIVING:
            if api_state == "BOARDING":
                # Real train started boarding but model hasn't arrived yet → catch up!
                return State.RUNNING_TO_STATION

        elif s == State.AT_STATION_WAITING:
            if api_state == "BOARDING":
                return State.AT_STATION_VALID

        elif s == State.RUNNING_TO_STATION:
            if api_state == "DRIVING":
                # Real train departed before we arrived
                if self._is_fasanenpark():
                    return State.DRIVING_TO_NONAME
                else:
                    return State.DRIVING

        # No transition for: DRIVING_TO_NONAME (waits for HALL)
        return None

    def _transition_on_hall(self) -> State | None:
        """Determine next state based on HALL sensor. Returns None if no transition."""
        s = self.state

        if s == State.DRIVING:
            if self.last_api_state == "BOARDING":
                return State.AT_STATION_VALID
            else:  # last_api_state == "DRIVING"
                return State.AT_STATION_WAITING

        elif s == State.DRIVING_TO_NONAME:
            return State.WAITING_AT_NONAME

        elif s == State.RUNNING_TO_STATION:
            # If HALL triggers while still in RUNNING_TO_STATION, real train must still be boarding
            # (if it departed, we'd already be in DRIVING state via API transition)
            return State.AT_STATION_VALID

        # No transition for: WAITING_AT_NONAME (model parked), AT_STATION_* (model stopped)
        return None

    # ── Entry actions ───────────────────────────────────────────────────

    def _enter_state(self, new_state: State, from_state: State, coordinates: list | None = None):
        """Execute entry actions and apply outputs for the new state."""
        now = datetime.now().strftime('%H:%M:%S')

        # ── Data processing (entry actions) ──
        if new_state == State.AT_STATION_VALID:
            if coordinates:
                # Re-sync to GPS on every boarding — corrects drift if the API had a
                # blackout and we miscounted the index during that gap.
                self._gps_sync_station(coordinates, now)
            elif from_state == State.WAITING_AT_NONAME:
                # Very first boarding with no GPS: default to first station
                self.current_station_index = 0
                print(f"[{now}] ⚠️  No GPS on first boarding, defaulting to first station: {self.stations[0]['name']}")
            # else: no GPS but not first boarding — trust existing counter

        elif new_state == State.DRIVING:
            if from_state == State.WAITING_AT_NONAME:
                # Pre-start: real train is already driving, send the model from
                # noname to the first HALL position so it's ready when the real
                # train boards.  Station index comes from GPS (approaching station).
                if coordinates:
                    lat = coordinates[1]
                    self.current_station_index = self._find_approaching_station(lat)
                elif self.current_station_index is None:
                    self.current_station_index = 0
                self.travel_speed = self._calculate_speed_for_time(self.NONAME_TRAVEL_SECONDS)
                # Ignore hall sensor: model loops freely until real train boards,
                # at which point RUNNING_TO_STATION entry sends loops=0 to "arm" the magnet.
                self.current_loops = -1
                print(f"[{now}] 🚀 Pre-starting model from noname "
                      f"(approaching {self.stations[self.current_station_index]['name']})")
            else:
                # Normal departure: increment index to point to our destination (next station)
                if self.current_station_index is not None:
                    self.current_station_index += 1
                    if self.current_station_index >= len(self.stations):
                        self.current_station_index = len(self.stations) - 1
                        print(f"[{now}] ⚠️  Station index overflow, clamped to last station")
                # Calculate speed and matching loop count from travel time.
                self.travel_speed = self._calculate_speed()
                self.current_loops = self._calculate_loops()

        elif new_state == State.DRIVING_TO_NONAME:
            # Fixed speed for return to noname
            self.travel_speed = self._calculate_speed_for_time(self.NONAME_TRAVEL_SECONDS)
            # TODO: derive loop count from config
            self.current_loops = 0

        elif new_state == State.RUNNING_TO_STATION:
            # Real train is boarding at the station the model is running toward.
            # current_station_index already points there (was incremented on DRIVING entry).
            # Re-sync from GPS in case we drifted — same logic as AT_STATION_VALID.
            if coordinates:
                self._gps_sync_station(coordinates, now)
            # Arm the hall sensor: model stops at the next pass ("activate the magnet").
            # Also handles the pre-start case where the model was looping with loops=-1.
            self.current_loops = 0

        elif new_state == State.WAITING_AT_NONAME:
            self.current_station_index = None
            self.last_api_state = None
            self.eta_to_fasanenpark = None
            self.station.send_eta(None)

        # ── State transition ──
        old_name = self.state.name
        self.state = new_state

        print(f"[{now}] 🔄 {old_name} → {new_state.name}")

        # ── Apply Moore outputs ──
        self._apply_outputs()

    def _apply_outputs(self):
        """Apply outputs for current state (Moore: outputs depend only on state)."""
        s = self.state
        now = datetime.now().strftime('%H:%M:%S')

        if s == State.WAITING_AT_NONAME:
            self.station.send_clear()
            print(f"[{now}]   → Model: (already stopped by MCU) | Station: clear (waiting for next train)")

        elif s == State.AT_STATION_VALID:
            name = self._current_station_name()
            self.station.send_station(name, State.AT_STATION_VALID.name)
            eta_str = self._eta_str()
            print(f"[{now}]   → Model: (already stopped by MCU) | Station: {name} ✅{eta_str}")

        elif s == State.AT_STATION_WAITING:
            name = self._current_station_name()
            self.station.send_station(name, State.AT_STATION_WAITING.name)
            eta_str = self._eta_str()
            print(f"[{now}]   → Model: (already stopped by MCU) | Station: {name} ❌ (waiting){eta_str}")

        elif s == State.DRIVING:
            self.model.send_loops(self.current_loops)
            self.model.send_speed(self.travel_speed)
            name = self._current_station_name()
            self.station.send_station(name, State.DRIVING.name)
            eta_str = self._eta_str()
            print(f"[{now}]   → Model: SPEED:{self.travel_speed:.2f} | Station: → {name}{eta_str}")

        elif s == State.DRIVING_TO_NONAME:
            self.model.send_loops(self.current_loops)
            self.model.send_speed(self.travel_speed)
            self.station.send_clear()
            print(f"[{now}]   → Model: SPEED:{self.travel_speed:.2f} | Station: clear (→ noname)")

        elif s == State.RUNNING_TO_STATION:
            self.model.send_loops(self.current_loops)
            self.model.send_speed(self.MAX_SPEED)
            name = self._current_station_name()
            self.station.send_station(name, State.RUNNING_TO_STATION.name)
            eta_str = self._eta_str()
            print(f"[{now}]   → Model: SPEED:1.0 (catch-up!) | Station: → {name}{eta_str}")

    # ── Helpers ─────────────────────────────────────────────────────────

    def _is_fasanenpark(self) -> bool:
        """Check if we're currently at the last station (Fasanenpark)."""
        if self.current_station_index is None:
            return False
        return self.current_station_index >= len(self.stations) - 1

    def _current_station_name(self) -> str:
        if self.current_station_index is not None and self.current_station_index < len(self.stations):
            return self.stations[self.current_station_index]['name']
        return "???"

    def _next_station_name(self) -> str:
        if self.current_station_index is not None:
            nxt = self.current_station_index + 1
            if nxt < len(self.stations):
                return self.stations[nxt]['name']
        return "noname"

    def _calculate_speed(self) -> float:
        """Calculate model speed from travel_times for the current station segment.
        
        When driving, current_station_index points to our DESTINATION.
        We need travel_time from the PREVIOUS station TO our destination.
        So we use stations[current_station_index - 1]['travel_time_to_next'].
        """
        if self.current_station_index is None or self.current_station_index <= 0:
            return self.MIN_SPEED
        
        if self.current_station_index >= len(self.stations):
            return self.MIN_SPEED
        
        # Get the previous station's travel_time_to_next (which is the time TO our destination)
        prev_station = self.stations[self.current_station_index - 1]
        travel_time = prev_station.get('travel_time_to_next')
        
        if not travel_time or travel_time <= 0:
            return self.MIN_SPEED
        return self._calculate_speed_for_time(travel_time)

    def _calculate_loops(self) -> int:
        """Calculate how many extra hall-sensor passes to allow for the current segment.

        Speed is set by _calculate_speed() to match travel_time for exactly 1 loop
        when in the [MIN_SPEED, MAX_SPEED] range.  When speed is clamped (segment too
        long for MIN_SPEED), the model would arrive far too early with 1 loop, so we
        add extra loops until the total model time best approximates travel_time.

        Examples (TRACK_LOOP_SECONDS=60, MIN_SPEED=0.35):
          120 s segment → speed=0.50 → loop_time=120 s → 0 extra loops
          360 s segment → speed clamped to 0.35 → loop_time≈171 s → round(360/171)-1 = 1 extra loop
        """
        if self.current_station_index is None or self.current_station_index <= 0:
            return 0
        prev_station = self.stations[self.current_station_index - 1]
        travel_time = prev_station.get('travel_time_to_next')
        if not travel_time or travel_time <= 0:
            return 0
        loop_time = self.TRACK_LOOP_SECONDS / self.travel_speed
        return max(0, round(travel_time / loop_time) - 1)

    def _calculate_speed_for_time(self, seconds: float) -> float:
        """speed = TRACK_LOOP_SECONDS / travel_time, clamped to [MIN, MAX]."""
        speed = self.TRACK_LOOP_SECONDS / seconds
        return max(self.MIN_SPEED, min(self.MAX_SPEED, speed))

    def _eta_str(self) -> str:
        """Human-readable ETA string for log output, e.g. ' (ETA Fasanenpark: 19:04)'."""
        if self.eta_to_fasanenpark is None:
            return ""
        return f" (ETA Fasanenpark: {datetime.fromtimestamp(self.eta_to_fasanenpark).strftime('%H:%M')})"

    def _gps_sync_station(self, coordinates: list, now: str):
        """Re-sync current_station_index from GPS coordinates (called on BOARDING events).

        If the GPS latitude is outside the Holzkirchen–Fasanenpark range the train
        is not on our route — sets restart_event so the tracking loop drops it.
        """
        lat = coordinates[1]
        _PADDING = 0.0045  # ~500 m in degrees latitude
        route_lat_min = self.stations[0].get('lat', 0) - _PADDING
        route_lat_max = self.stations[-1].get('lat', 0) + _PADDING
        if not (route_lat_min <= lat <= route_lat_max):
            print(f"[{now}] 🚨 GPS lat {lat:.5f} is outside route range "
                  f"[{route_lat_min:.5f}, {route_lat_max:.5f}] — train off-route, requesting restart")
            self.restart_event.set()
            return
        idx = self._find_nearest_station(coordinates)
        if idx != self.current_station_index:
            old_name = self.stations[self.current_station_index]['name'] if self.current_station_index is not None else "?"
            print(f"[{now}] 📍 GPS re-sync: {old_name} (idx {self.current_station_index}) → {self.stations[idx]['name']} (idx {idx})")
        else:
            print(f"[{now}] 📍 GPS confirmed: {self.stations[idx]['name']} (index {idx})")
        self.current_station_index = idx

    def _find_approaching_station(self, lat: float) -> int:
        """Find the station the northbound train is approaching based on latitude.

        The S3 Holzkirchen→Fasanenpark route is strictly south→north (increasing lat),
        so the approaching station is the first one whose lat exceeds the train's lat.
        If the train is already past all stations, returns the last index.
        """
        for i, station in enumerate(self.stations):
            if station.get('lat', 0) > lat:
                return i
        return len(self.stations) - 1

    def _find_nearest_station(self, coordinates: list) -> int:
        """Find the station closest to the given [lon, lat] coordinates."""
        if not coordinates or len(coordinates) < 2:
            return 0

        lon, lat = coordinates[0], coordinates[1]
        best_idx = 0
        best_dist = float('inf')

        for i, station in enumerate(self.stations):
            slat = station.get('lat', 0)
            slon = station.get('lon', 0)
            dist = self._haversine(lat, lon, slat, slon)
            if dist < best_dist:
                best_dist = dist
                best_idx = i

        return best_idx

    @staticmethod
    def _haversine(lat1, lon1, lat2, lon2) -> float:
        """Distance in meters between two lat/lon points."""
        R = 6371000
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlam = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    # ── Status ──────────────────────────────────────────────────────────

    def status(self) -> str:
        """Human-readable status string."""
        if self.state in (State.DRIVING, State.DRIVING_TO_NONAME, State.RUNNING_TO_STATION):
            station = f"{self._current_station_name()} → {self._next_station_name()}"
        elif self.current_station_index is not None:
            station = self._current_station_name()
        else:
            station = "none"
        return (
            f"State: {self.state.name} | Station: {station} "
            f"(idx={self.current_station_index}) | API: {self.last_api_state} | Speed: {self.travel_speed:.2f}"
        )
