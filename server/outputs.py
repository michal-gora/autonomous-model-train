"""
Output interfaces for model train and station display.

These are "dumb I/O" - they just forward commands.
Start with PrintStub implementations for testing, replace with WiFi/GPIO later.
"""

from abc import ABC, abstractmethod


# ── Model Train Interface ───────────────────────────────────────────────

class ModelOutput(ABC):
    """Abstract interface: Server → Model Train."""

    @abstractmethod
    def send_speed(self, speed: float):
        """Send SPEED:x command (0.0 to 1.0)."""
        ...

    @abstractmethod
    def send_stop(self):
        """Send STOP command."""
        ...

    @abstractmethod
    def send_loops(self, count: int):
        """Send LOOPS:N command.
        N=0: stop on first hall pass.
        N>0: pass through N extra times before stopping.
        N<0: ignore hall sensor (run indefinitely).
        """
        ...


class PrintModelOutput(ModelOutput):
    """Print-stub: logs commands to console."""

    def send_speed(self, speed: float):
        pass  # State machine already prints; avoid double-logging

    def send_stop(self):
        pass  # State machine already prints

    def send_loops(self, count: int):
        pass  # State machine already prints


# ── Station Display Interface ───────────────────────────────────────────

class StationOutput(ABC):
    """Abstract interface: Server → Station Display."""

    @abstractmethod
    def send_station(self, name: str, state: str):
        """Send STATION:name:STATE (e.g. STATION:Marienplatz:AT_STATION_VALID)."""
        ...

    @abstractmethod
    def send_eta(self, arrival_unix: int | None):
        """Send ETA:<unix_timestamp> or ETA:none when there is no tracked train."""
        ...

    @abstractmethod
    def send_clear(self):
        """Send STATION:clear."""
        ...


class PrintStationOutput(StationOutput):
    """Print-stub: logs commands to console."""

    def send_station(self, name: str, state: str):
        pass  # State machine already prints

    def send_eta(self, arrival_unix: int | None):
        pass  # State machine already prints

    def send_clear(self):
        pass  # State machine already prints
