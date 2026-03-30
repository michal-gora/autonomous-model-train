import controller.wifi as wifi
import socket
import sys
import time
import errno
import machine
from machine import Pin
from machine import PWM

# ============================================================
# CONFIGURATION
# ============================================================
# Network
SERVER_IP = "192.168.178.26"
SERVER_PORT = 8080

# Watchdog timers (must coordinate with server settings)
PING_INTERVAL = 10      # seconds - how often MCU sends PING
PONG_TIMEOUT = 3        # seconds - if no PONG received, reconnect
RECONNECT_DELAY = 5    # seconds - wait before reconnecting

# Hardware pins
LED_PIN = "P5_3"
PWM_PIN = "P9_7"
REVERSER_PIN = "P9_6"
HALL_PIN = "P9_0"

# Hall sensor settings
# Debouncing logic:
#   Falling edge IRQ : start timing (set hall_measuring, record hall_fall_time)
#   Rising edge IRQ  : cancel if not yet confirmed (pin went HIGH = noise)
#   Main loop        : if pin is STILL LOW after HALL_MIN_LOW_MS → confirmed
# This means HALL is sent while the magnet is present, not after it leaves.
# Noise spikes go HIGH before the main loop check and are cancelled by the
# rising-edge IRQ.
HALL_MIN_LOW_MS = 5    # ms – pin must stay LOW this long to confirm a trigger
HALL_STARTUP_DELAY = 1  # seconds - ignore hall triggers after train starts / after each pass-through
# ============================================================

# Global state
hall_triggered: bool = False  # set by rising-edge IRQ when LOW duration is long enough
hall_measuring: bool = False  # True after a falling edge, reset after rising edge
hall_fall_time: int = 0       # time.ticks_ms() recorded at falling edge
hall_loop_config: int = 0             # persistent setting, written by LOOPS:N command
                                      # 0 = stop on first pass, N>0 = N extra loops, <0 = ignore hall
hall_loops_remaining: int = 0         # runtime countdown, reset to hall_loop_config on each start
train_started_at: float = 0.0    # timestamp when train last started (speed > 0)
current_speed: float = 0.0
final_speed: float = 0.0
braking: bool = False            # True while hall-triggered deceleration is active
brake_step: float = 0.0          # PWM decrease per 10 ms tick during braking

# ── Tunable motion parameters ────────────────────────────────────────
TRACTION_MIN: float = 0.2        # PWM below which the motor stalls; treated as zero
LINEAR_ACCEL_STEP: float = 0.005 # PWM change per 10 ms tick for normal speed ramps
                                 # 0→1 in ~2 s; increase for snappier, decrease for smoother
BRAKE_DEAD_ZONE: float = 0.13    # effective zero for braking physics; tune independently of
                                 # TRACTION_MIN to equalise stopping distance across speeds.
                                 # If slow speeds overshoot, raise this value (e.g. 0.27).
                                 # brake_step = (v₀ - BRAKE_DEAD_ZONE)² * BRAKE_DECEL / 100
BRAKE_DECEL: float = 0.88        # braking strength coefficient (dimensionless, server-tunable)
                                 # higher = shorter braking distance, lower = longer

is_led_on = True
led_pin = LED_PIN
pwm_pin = PWM_PIN
reverser_pin = REVERSER_PIN

pwm = PWM(pwm_pin, freq=1000, duty_u16=0)
reverser = Pin(reverser_pin, Pin.OUT)
hall_sensor = Pin(HALL_PIN, Pin.IN, Pin.PULL_UP)
led = None

def toggle_led():
    global is_led_on, led
    if led == None:
        print(f"Error: led is None")
        return
    is_led_on = not is_led_on
    led.value(is_led_on)
    print(f"LED is now {'ON' if is_led_on else 'OFF'}")
    
def set_speed(speed: float):
    """Set the target speed. The main loop ramps current_speed toward it linearly.
    Cancels any active hall-triggered braking — a server command always takes priority.

    Args:
        speed: [0, 1], where 1.0 = full PWM.
    """
    global pwm, train_started_at, current_speed, final_speed, hall_loops_remaining, hall_loop_config, braking

    speed = max(0., min(speed, 1.))

    # Cancel hall-triggered braking if the server overrides the speed
    braking = False

    # Track transition from stopped to moving (reset watchdog timers)
    if current_speed == 0.0 and speed > 0.0:
        train_started_at = time.time()
        hall_loops_remaining = hall_loop_config
        print(f"Train started at {train_started_at}, loops set to {hall_loops_remaining}")

    final_speed = speed
    print(f"Target speed set to {speed:.2f}")
    
def set_reverser(reversed : bool):
    print("Set reverser to", reversed)
    global reverser
    reverser.value(reversed)

def hall_fall_interrupt(pin):
    """Falling edge: pin went LOW. Start timing.
    Ignored if hall_triggered is already set (main loop hasn't processed yet)
    or if we are already measuring a previous edge.
    """
    global hall_measuring, hall_fall_time
    if hall_triggered or hall_measuring:
        return
    hall_measuring = True
    hall_fall_time = time.ticks_ms()

def hall_rise_interrupt(pin):
    """Rising edge: pin went HIGH. Check if the LOW duration was long enough:
    - Yes → real trigger, set hall_triggered (main loop may not have seen it yet)
    - No  → too short, was just noise, discard
    """
    global hall_triggered, hall_measuring
    if hall_measuring:
        hall_measuring = False
        if time.ticks_diff(time.ticks_ms(), hall_fall_time) >= HALL_MIN_LOW_MS:
            hall_triggered = True  # fast pass: confirmed on the way out
        # else: too short – noise, discard

            
def start_socket_client():
    global hall_triggered, hall_measuring, hall_loops_remaining, hall_loop_config, train_started_at, current_speed, final_speed, braking, brake_step, BRAKE_DECEL, BRAKE_DEAD_ZONE
    
    s = None
    last_ping_sent: float = 0.0
    waiting_for_pong: bool = False
    
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
                
                print(f"Connected to server at {SERVER_IP}:{SERVER_PORT}")
                
                # Send HELLO handshake
                s.write(b"HELLO:MODEL\n")
                print("Sent HELLO:MODEL")
                
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
        
        # Main communication loop
        try:
            # Hall sensor check
            # Slow pass: pin still LOW and enough time has elapsed → confirm now.
            # Fast pass: already confirmed by rising-edge IRQ, hall_triggered is set.
            if hall_measuring and time.ticks_diff(time.ticks_ms(), hall_fall_time) >= HALL_MIN_LOW_MS:
                hall_measuring = False
                hall_triggered = True

            if hall_triggered:
                hall_triggered = False

                current_time = time.time()
                time_since_start = current_time - train_started_at

                if time_since_start < HALL_STARTUP_DELAY:
                    print(f"Ignoring hall trigger during startup/cooldown (t={time_since_start:.1f}s)")
                elif hall_loops_remaining < 0:
                    # Infinite mode – ignore hall sensor
                    pass
                elif hall_loops_remaining == 0:
                    # Begin hall-triggered braking. HALL is sent to the server once the
                    # train physically stops, not at the magnet trigger point.
                    if current_speed > 0.0:
                        braking = True
                        # Use (v - BRAKE_DEAD_ZONE)² so the calibrated effective-zero
                        # is excluded, giving constant physical stopping distance.
                        # BRAKE_DEAD_ZONE is tuned independently of TRACTION_MIN:
                        # raise it if slow speeds overshoot, lower it if they stop early.
                        effective = max(0.0, current_speed - BRAKE_DEAD_ZONE)
                        brake_step = effective ** 2 * BRAKE_DECEL / 100
                        print(f"Braking: entry={current_speed:.3f}, effective={effective:.3f}, step={brake_step:.5f}/tick")
                    else:
                        # Already stopped — notify server immediately
                        try:
                            s.write(b"HALL\n")
                            print("Sent HALL – train already stopped")
                        except OSError as e:
                            print(f"Failed to send HALL: {e}")
                            raise
                else:
                    # More loops to go – apply cooldown and decrement
                    hall_loops_remaining -= 1
                    train_started_at = time.time()
                    print(f"Pass-through, cooldown reset, {hall_loops_remaining} loop(s) remaining")
            
            # Speed control (runs every 10 ms)
            if braking:
                # Hall-triggered deceleration: quadratic profile → constant stopping distance
                current_speed -= brake_step
                if current_speed <= TRACTION_MIN:
                    # Motor has stalled / below traction — train is stopped
                    current_speed = 0.0
                    final_speed = 0.0
                    braking = False
                    try:
                        s.write(b"HALL\n")
                        print("Sent HALL – train stopped after braking")
                    except OSError as e:
                        print(f"Failed to send HALL: {e}")
                        raise
                pwm.duty_u16(int(current_speed * 65535.0) if current_speed >= TRACTION_MIN else 0)
            else:
                # Normal speed ramp: linear acceleration / deceleration
                speed_diff = final_speed - current_speed
                if speed_diff > 0:
                    current_speed = min(final_speed, current_speed + LINEAR_ACCEL_STEP)
                    # Jump over the stall zone when starting from rest
                    if 0 < current_speed < TRACTION_MIN:
                        current_speed = TRACTION_MIN
                elif speed_diff < 0:
                    current_speed = max(final_speed, current_speed - LINEAR_ACCEL_STEP)
                    if current_speed < TRACTION_MIN:
                        current_speed = 0.0
                        final_speed = 0.0  # snap to zero — motor can't run this slow
                pwm.duty_u16(int(current_speed * 65535.0) if current_speed >= TRACTION_MIN else 0)
            
            
            # Watchdog: Check if we need to send PING
            current_time = time.time()
            if current_time - last_ping_sent >= PING_INTERVAL:
                try:
                    s.write(b"PING\n")
                    last_ping_sent = current_time
                    waiting_for_pong = True
                    print("Sent PING")
                except OSError as e:
                    print(f"Failed to send PING: {e}")
                    raise
            
            # Watchdog: Check if we're waiting for PONG and it's overdue
            if waiting_for_pong and (current_time - last_ping_sent > PONG_TIMEOUT):
                print(f"No PONG received within {PONG_TIMEOUT}s after PING - reconnecting")
                try:
                    s.close()
                except:
                    pass
                s = None
                time.sleep(RECONNECT_DELAY)
                continue
            
            # Read line (non-blocking)
            raw_line = b""
            try:
                raw_line = s.readline()
            except OSError as e:
                if e.args[0] != errno.EAGAIN:
                    raise
                # No data available
            
            # Check if connection was closed (readline returns empty bytes)
            if raw_line == b"":
                # Empty read could mean closed connection, check again
                try:
                    # Try another read to confirm
                    test = s.recv(1)
                    if test == b"":
                        print("Server closed connection - reconnecting")
                        try:
                            s.close()
                        except:
                            pass
                        s = None
                        time.sleep(RECONNECT_DELAY)
                        continue
                except OSError as e:
                    if e.args[0] != errno.EAGAIN:
                        # Connection is dead
                        print("Connection lost - reconnecting")
                        try:
                            s.close()
                        except:
                            pass
                        s = None
                        time.sleep(RECONNECT_DELAY)
                        continue
            
            # Only process if we got data
            if raw_line:
                line_str = raw_line.decode('utf-8', 'ignore').strip()
                
                """
                Train Control Protocol:
                    RegEx: <command name>[":"<value>|";"]
                    Commands should be snake_case.
                    Values are preferred as int > float > String
                    Summary:
                    Commands that simply send a signal without passing arguments, end with ";"
                    Commands that pass an argument, separate name from value with a ":"
                """
                if line_str == "PONG":
                    waiting_for_pong = False
                    print("Received PONG")
                elif line_str == "LED_BUTTON":
                    print("Received Button")
                    toggle_led()
                    s.write(b"LED toggled!\n")
                elif line_str.startswith("SPEED:"):
                    print("Received slider")
                    s.write(b"Slider received!\n")
                    try:
                        value = float(line_str.split(':')[1])
                        set_speed(value)
                        print(f"Slider value: {value}")
                    except (IndexError, ValueError):
                        print("Invalid slider format")
                elif line_str.startswith("REVERSER:"):
                    reverser_state = bool(int(line_str.split(":")[1]))
                    print("Reverser state:", reverser_state)
                    set_reverser(reverser_state)
                elif line_str.startswith("LOOPS:"):
                    try:
                        loops = int(line_str.split(":")[1])
                        hall_loop_config = loops    # negative=infinite, 0=stop immediately, N=extra loops
                        hall_loops_remaining = loops  # take effect immediately, not just on next start
                        print(f"Hall loop count set to {hall_loop_config}")
                    except (IndexError, ValueError):
                        print("Invalid LOOPS format")
                elif line_str.startswith("BRAKE_DECEL:"):
                    try:
                        BRAKE_DECEL = float(line_str.split(":")[1])
                        print(f"Brake decel set to {BRAKE_DECEL}")
                    except (IndexError, ValueError):
                        print("Invalid BRAKE_DECEL format")
                elif line_str.startswith("BRAKE_DEAD_ZONE:"):
                    try:
                        BRAKE_DEAD_ZONE = float(line_str.split(":")[1])
                        print(f"Brake dead zone set to {BRAKE_DEAD_ZONE}")
                    except (IndexError, ValueError):
                        print("Invalid BRAKE_DEAD_ZONE format")
                else:
                    print(f"Unknown command: {line_str}")
                # Ignore unknown messages silently (could be ACK or other server messages)

        except OSError as e:
            code = e.args[0]
            if code == errno.ECONNRESET:
                print("Connection reset/broken pipe – reconnecting")
                try:
                    s.close()
                except:
                    pass
                s = None
                time.sleep(RECONNECT_DELAY)
                continue
            elif code == errno.EAGAIN:
                # No data this cycle — skip work
                pass
            elif code == errno.ETIMEDOUT:
                print("Connection timed out - reconnecting")
                try:
                    s.close()
                except:
                    pass
                s = None
                time.sleep(RECONNECT_DELAY)
                continue
            else:
                print(f"Socket error: {e} - reconnecting")
                try:
                    s.close()
                except:
                    pass
                s = None
                time.sleep(RECONNECT_DELAY)
                continue

        # Do other tasks here...
        time.sleep_ms(10)

def main():
    global led, is_led_on
    led_pwm = PWM(led_pin, freq=4, duty_u16=35555)
    set_reverser(True)
    set_speed(0.0)
    
    # Set up hall effect sensor with interrupt
    # MicroPython only supports one IRQ per pin, so we combine both edges into
    # one handler and dispatch by reading the current pin value.
    hall_sensor.irq(trigger=Pin.IRQ_FALLING | Pin.IRQ_RISING,
                    handler=lambda p: hall_fall_interrupt(p) if p.value() == 0 else hall_rise_interrupt(p))
    print(f"Hall sensor initialized on {HALL_PIN} (min LOW: {HALL_MIN_LOW_MS} ms)")

    # Connect to WiFi with retry
    max_wifi_retries = 5
    wifi_retry_delay = 3  # seconds
    
    for attempt in range(max_wifi_retries):
        print(f"Connecting to WiFi... (attempt {attempt + 1}/{max_wifi_retries})")
        try:
            if wifi.network_connect():
                print("WiFi connected")
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
        # All retries failed
        print("Failed to connect to WiFi after all retries. Restarting...")
        led_pwm.deinit()
        time.sleep(5)
        machine.reset()

    led_pwm.deinit()
    led = Pin(led_pin, Pin.OUT)
    led.value(True)
    is_led_on = True

    # Start client with exception handling
    while True:
        try:
            start_socket_client()
        except Exception as e:
            print(f"Fatal error in socket client: {e}")
            print("Restarting in 5 seconds...")
            time.sleep(5)
            import machine
            machine.reset()

if __name__ == "__main__":
    main()

### TODO:
# - Add error handling for malformed commands
# - Finally adjust it in sbahn server script to send speed and reverser commands based on state machine logic