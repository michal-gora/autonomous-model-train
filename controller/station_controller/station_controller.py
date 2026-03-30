import wifi
import ntptime
import socket
import time
from time import sleep_ms
import errno
import machine
from machine import I2C, Pin
from mp_i2c_lcd1602 import I2C_LCD1602

def unix_time() -> int:
    """Return current Unix timestamp in seconds (correct after NTP sync).
    Reads from the RTC (set by ntptime.settime()) and converts via time.mktime().
    On this MicroPython port, time.mktime() returns seconds since the Unix epoch
    (1970-01-01), so no offset adjustment is needed.
    """
    dt = machine.RTC().datetime()
    # dt = (year, month, day, weekday, hour, minute, second, subsecond)
    return time.mktime((dt[0], dt[1], dt[2], dt[4], dt[5], dt[6], dt[3], 0))

# ============================================================
# CONFIGURATION
# ============================================================
# Network
SERVER_IP = "192.168.178.26"
SERVER_PORT = 8081

# Watchdog timers (must coordinate with server settings)
PING_INTERVAL = 10      # seconds - how often MCU sends PING
PONG_TIMEOUT = 3        # seconds - if no PONG received, reconnect
RECONNECT_DELAY = 5     # seconds - wait before reconnecting

# Hardware
RESTART_BUTTON_PIN = "P0_4"  # active-LOW button with internal pull-up; adjust pin as needed
BUTTON_DEBOUNCE_MS = 300     # minimum ms between accepted presses
# ============================================================


# ============================================================
# DISPLAY WRAPPER FUNCTIONS
# Replace these with your actual display implementation.
# ============================================================
i2c = I2C(sda=Pin("P6_1"), scl=Pin("P6_0"))
LCD = I2C_LCD1602(i2c)

# Restart button (active-LOW, internal pull-up)
restart_button = Pin(RESTART_BUTTON_PIN, Pin.IN, Pin.PULL_UP)
_last_button_press_ms: int = 0  # debounce tracker

def display_clear(arg: int = -1):
    """Clear the display."""
    if arg == 0:
        LCD.puts(" " * 16, y=0)
    elif arg == 1:
        LCD.puts(" " * 15, y=1)
    elif arg == 2:
        LCD.puts(" ", x=15, y=1)
    else:
        LCD.clear()
    print("Display: clear")

def display_text(line: str, arg : int = 0):
    """Show text on the display. line1 = top row, line2 = bottom row.
    Args:
        line: The text to display (will be truncated to fit).
        arg: Which element to update (0 = top row, 1 = bottom row, 2 = status indicator bottom right)
    """
    display_clear(arg)
    if arg == 0:
        LCD.puts(f"{line.strip()[:16]}", y=0)
    elif arg == 1:
        LCD.puts(f"{line.strip()[:15]}", y=1)
    elif arg == 2:
        LCD.puts(line.strip()[:1], x=15, y=1)
    print(f"Display: [{line}] with arg={arg}")

def display_eta(remaining_seconds: int):
    """Show remaining time until train arrives at Fasanenpark."""
    mins = remaining_seconds // 60
    secs = remaining_seconds % 60
    if mins > 99:
        display_text("ETA: >99m", 1)
    display_text(f"ETA: {str(mins)[:3]}m {secs:02d}s", 1)
    print(f"ETA display: {mins}m {secs:02d}s remaining")


def display_init():
    """Initialize the display hardware. Called once at startup."""
    display_clear()
    display_text("Hello, world!")
    print("Display init")


# ============================================================


def start_socket_client():
    s = None
    # time.time() returns epoch seconds after NTP sync — use it for all interval
    # tracking. ticks_ms/ticks_us are unusable here: their TICKS_PERIOD is only
    # 15000 (shared for both ms and µs on this port), so ticks_diff saturates
    # at 7.5 s — less than the 10 s PING_INTERVAL.
    last_ping_sent: int = 0
    waiting_for_pong: bool = False
    eta_unix: int | None = None
    last_eta_display: int = 0    # raw ticks_ms() value
    ETA_DISPLAY_INTERVAL = 10

    while True:
        # Connection/reconnection loop
        if s is None:
            try:
                print(f"Attempting to connect to {SERVER_IP}:{SERVER_PORT}...")
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.settimeout(5.0)  # 5 second timeout for connect
                s.connect((SERVER_IP, SERVER_PORT))
                s.setblocking(False)  # Set non-blocking after connect

                print(f"Connecting to server at {SERVER_IP}:{SERVER_PORT}")

                # Send HELLO handshake
                s.write(b"HELLO:STATION\n")
                print("Sent HELLO:STATION")

                # Reset watchdog timers
                last_ping_sent = time.time()
                waiting_for_pong = False

            except OSError as e:
                print(f"Connection failed: {e}")
                if s:
                    try:
                        s.close()
                    except:
                        pass
                    s = None
                print(f"Retrying in {RECONNECT_DELAY} seconds...")
                time.sleep(RECONNECT_DELAY)
                continue

        # Main communication loop — all timing via plain time.time() integer reads.
        try:
            # Restart button check (active-LOW, debounced)
            now_ms = time.ticks_ms()
            if (restart_button.value() == 0
                    and time.ticks_diff(now_ms, _last_button_press_ms) > BUTTON_DEBOUNCE_MS):
                global _last_button_press_ms
                _last_button_press_ms = now_ms
                print("Button pressed — sending RESTART")
                try:
                    s.write(b"RESTART\n")
                except OSError:
                    pass  # will be caught below if socket is dead

            now = time.time()
            if now - last_ping_sent >= PING_INTERVAL:
                try:
                    s.write(b"PING\n")
                    last_ping_sent = now
                    waiting_for_pong = True
                    print("Sent PING")
                except OSError as e:
                    print(f"Failed to send PING: {e}")
                    raise

            # Watchdog: check PONG timeout
            if waiting_for_pong and now - last_ping_sent > PONG_TIMEOUT:
                print(f"No PONG received within {PONG_TIMEOUT}s after PING - reconnecting")
                try:
                    s.close()
                except:
                    pass
                s = None
                time.sleep(RECONNECT_DELAY)
                continue

            # Read line (non-blocking).
            # raw_line = None means EAGAIN (no data yet) — don't touch the socket again.
            # raw_line = b"" means readline() returned empty → peer closed the connection.
            # raw_line = bytes  means a complete newline-terminated line was received.
            raw_line = None
            try:
                data = s.readline()
                raw_line = data  # b"" = closed, or actual line
            except OSError as e:
                if e.args[0] != errno.EAGAIN:
                    raise
                # EAGAIN — no complete line buffered yet, raw_line stays None

            # Check if connection was closed by peer
            if raw_line == b"":
                print("Server closed connection - reconnecting")
                try:
                    s.close()
                except:
                    pass
                s = None
                time.sleep(RECONNECT_DELAY)
                continue

            # Process received line
            if raw_line:
                line_str = raw_line.decode('utf-8', 'ignore').strip()

                if line_str == "PONG":
                    waiting_for_pong = False
                    print("Received PONG")

                elif line_str.startswith("STATION:"):
                    # STATION:name:STATE  → display name with state
                    #   STATE = AT_STATION_VALID | AT_STATION_WAITING | DRIVING | RUNNING_TO_STATION
                    # STATION:clear       → clear display
                    parts = line_str.split(":", 2)
                    if len(parts) == 2 and parts[1] == "clear":
                        display_clear()
                    elif len(parts) == 3:
                        station_name = parts[1]
                        state = parts[2]
                        display_text(station_name, 0)
                        if state == "AT_STATION_VALID" or state == "RUNNING_TO_STATION":
                            display_text("B", 2)
                        else:
                            display_text(" ", 2)  # clear status indicator
                    else:
                        print(f"Unknown STATION format: {line_str}")

                elif line_str.startswith("ETA:"):
                    # ETA:<unix_timestamp>  → absolute arrival time at Fasanenpark
                    # ETA:none              → no tracked train, no ETA
                    value = line_str[4:]  # strip "ETA:"
                    if value == "none":
                        eta_unix = None
                        print("ETA: none")
                    else:
                        try:
                            eta_unix = int(value)
                            print(f"ETA: {eta_unix}")
                        except ValueError:
                            print(f"Invalid ETA value: {value}")
                            eta_unix = None

                elif line_str == "ACK":
                    display_text("Connected")
                    print("Successfully connected")
                # Ignore unknown messages silently (could be ACK or other server messages)

            # Periodically display remaining ETA
            now = time.time()
            if eta_unix is not None and now - last_eta_display >= ETA_DISPLAY_INTERVAL:
                remaining = eta_unix - unix_time()
                display_eta(remaining)
                last_eta_display = now
            elif eta_unix is None:
                display_clear(1)  # clear ETA line if no train tracked
        except OSError as e:
            code = e.args[0]
            if code == errno.ECONNRESET:
                print("Connection reset – reconnecting")
            elif code == errno.ETIMEDOUT:
                print("Connection timed out - reconnecting")
            elif code == errno.EAGAIN:
                pass  # No data this cycle
            else:
                print(f"Socket error: {e} - reconnecting")

            if code != errno.EAGAIN:
                try:
                    s.close()
                except:
                    pass
                s = None
                time.sleep(RECONNECT_DELAY)
                continue

        # Do other tasks here...
        time.sleep(0.1)


def main():
    display_init()

    # Connect to WiFi with retry
    max_wifi_retries = 5
    wifi_retry_delay = 3  # seconds

    for attempt in range(max_wifi_retries):
        print(f"Connecting to WiFi... (attempt {attempt + 1}/{max_wifi_retries})")
        try:
            if wifi.network_connect():
                print("WiFi connected")
                # Sync RTC via NTP so unix_time() returns correct Unix timestamps
                try:
                    print(f"Before: {time.ticks_ms() // 1000} seconds since boot, RTC datetime={machine.RTC().datetime()}")
                    ntptime.settime()
                    print(f"After: {time.ticks_ms() // 1000} seconds since boot, RTC datetime={machine.RTC().datetime()}") 
                    print(f"NTP sync done, unix_time={unix_time()}")
                except Exception as e:
                    print(f"NTP sync failed: {e} (ETA countdown will be wrong)")
                break
            else:
                print(f"WiFi connection failed")
                if attempt < max_wifi_retries - 1:
                    print(f"Retrying in {wifi_retry_delay} seconds...")
                    time.sleep(wifi_retry_delay)
        except Exception as e:
            print(f"WiFi error: {e}")
            if attempt < max_wifi_retries - 1:
                print(f"Retrying in {wifi_retry_delay} seconds...")
                time.sleep(wifi_retry_delay)
    else:
        print("Failed to connect to WiFi after all retries. Restarting...")
        time.sleep(5)
        machine.reset()

    # Start client with exception handling
    while True:
        try:
            start_socket_client()
        except Exception as e:
            print(f"Fatal error in socket client: {e}")
            print("Restarting in 5 seconds...")
            time.sleep(5)
            machine.reset()


if __name__ == "__main__":
    main()

