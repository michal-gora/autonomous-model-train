"""
TCP-based output for the station display.

Architecture: Server runs a plain TCP server. The station display (MicroPython
on PSoC 6) connects as a TCP client. Messages are newline-terminated text.

Protocol:
    Server → Station:  STATION:name:STATE\n  (STATE = AT_STATION_VALID | AT_STATION_WAITING | DRIVING | RUNNING_TO_STATION)  |   STATION:clear\n
    Station → Server:  HELLO:STATION\n  |   PING\n
    Server → Station:  ACK\n  (after HELLO)  |   PONG\n  (after PING)
"""

import asyncio
from outputs import StationOutput


# ============================================================
# CONFIGURATION
# ============================================================
STATION_TCP_PORT = 8081

# Watchdog timer (must coordinate with MCU settings)
# MCU sends PING every 10s, expects PONG within 3s
# Server should timeout if no PING received for 15s
PING_TIMEOUT = 15  # seconds - if no PING received from MCU, close connection
# ============================================================


class TcpStationOutput(StationOutput):
    """Plain-TCP output for station display."""

    def __init__(self):
        self.writer: asyncio.StreamWriter | None = None
        self.connected = False
        self._last_station_message: str | None = None  # cached for replay on reconnect
        self._last_eta_message: str | None = None      # cached for replay on reconnect

    def set_writer(self, writer: asyncio.StreamWriter):
        self.writer = writer
        self.connected = True
        print("🔌 Station display connected (TCP)")
        # Immediately replay last known state so the display is up to date
        for msg in (self._last_station_message, self._last_eta_message):
            if msg:
                try:
                    writer.write(msg.encode())
                    asyncio.create_task(writer.drain())
                    print(f"📤 → Station (replay): {msg.strip()}")
                except Exception as e:
                    print(f"❌ Error replaying state to station display: {e}")

    def disconnect(self):
        self.writer = None
        self.connected = False
        print("⚠️  Station display disconnected")

    def _write(self, message: str):
        """Send a message to the station display if connected (non-blocking)."""
        if self.writer and self.connected:
            try:
                self.writer.write(message.encode())
                asyncio.create_task(self.writer.drain())
            except Exception as e:
                print(f"❌ Error sending to station display: {e}")
                self.connected = False

    def send_station(self, name: str, state: str):
        """Send STATION:name:STATE."""
        self._last_station_message = f"STATION:{name}:{state}\n"
        self._write(self._last_station_message)
        print(f"📤 → Station: {name} ({state})")

    def send_eta(self, arrival_unix: int | None):
        """Send ETA:<unix_timestamp> or ETA:none."""
        self._last_eta_message = f"ETA:{arrival_unix}\n" if arrival_unix is not None else "ETA:none\n"
        self._write(self._last_eta_message)
        if arrival_unix is not None:
            from datetime import datetime
            eta_str = datetime.fromtimestamp(arrival_unix).strftime("%H:%M:%S")
        else:
            eta_str = "none"
        print(f"📤 → Station ETA: {eta_str}")

    def send_clear(self):
        """Send STATION:clear."""
        self._last_station_message = "STATION:clear\n"
        self._write(self._last_station_message)
        print(f"📤 → Station: clear")


async def tcp_station_server(station_output: TcpStationOutput, restart_event: asyncio.Event | None = None):
    """
    TCP server that accepts ONE station display connection at a time.
    The station sends newline-terminated commands; we send newline-terminated replies.
    """

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        print(f"📡 TCP connection from {peer} (station display)")
        
        last_ping_received = asyncio.get_running_loop().time()

        try:
            # First message must be HELLO:STATION
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            hello = line.decode().strip()

            if hello != "HELLO:STATION":
                print(f"❌ Expected HELLO:STATION, got: {hello!r}")
                writer.write(b"ERROR:expected HELLO:STATION\n")
                await writer.drain()
                writer.close()
                return

            # Acknowledge
            writer.write(b"ACK\n")
            await writer.drain()

            station_output.set_writer(writer)

            # Read loop — station display sends PING (and potentially status messages)
            while True:
                try:
                    # Check watchdog timeout
                    current_time = asyncio.get_running_loop().time()
                    if current_time - last_ping_received > PING_TIMEOUT:
                        print(f"⚠️  No PING received from station for {PING_TIMEOUT}s - closing connection")
                        break
                    
                    line = await asyncio.wait_for(reader.readline(), timeout=1.0)
                    if not line:
                        # Connection closed
                        break
                    msg = line.decode().strip()
                    if not msg:
                        continue

                    if msg == "PING":
                        last_ping_received = current_time
                        writer.write(b"PONG\n")
                        await writer.drain()
                        # Uncomment for debugging: print("📤 Sent PONG to station")
                    elif msg == "RESTART":
                        print("🔄 RESTART received from station display")
                        if restart_event is not None:
                            restart_event.set()
                        else:
                            print("⚠️  RESTART received but no restart_event configured")
                    else:
                        print(f"⚠️  Unknown message from station display: {msg!r}")
                        
                except asyncio.TimeoutError:
                    # No data received in 1s, continue to check watchdog
                    continue

        except asyncio.TimeoutError:
            print("❌ Station client timed out during handshake")
        except Exception as e:
            print(f"❌ TCP station error: {e}")
        finally:
            station_output.disconnect()
            try:
                writer.close()
            except:
                pass

    server = await asyncio.start_server(handle_client, "0.0.0.0", STATION_TCP_PORT)
    print(f"🌐 TCP station server listening on 0.0.0.0:{STATION_TCP_PORT}")
    print(f"   Station display should connect to: {STATION_TCP_PORT}/tcp")
    print(f"   Protocol: Send 'HELLO:STATION\\n', receive 'STATION:*\\n' commands")
    return server
