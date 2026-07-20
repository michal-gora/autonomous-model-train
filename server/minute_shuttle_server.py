#!/usr/bin/env python3
"""
Minute-Shuttle Mode Server

Simplest operating mode: no S-Bahn API tracking, no virtual train fallback.
The model train just shuttles back and forth, advancing exactly one station
(one magnet) every minute at a moderate constant speed.

Behaviour:
  - Every SHUTTLE_INTERVAL seconds, send LOOPS:0 + SPEED:DRIVE_SPEED so the
    model runs forward and stops automatically at the very next magnet.
  - The station display simply shows "Running" / the last known station name.

Usage:
    python minute_shuttle_server.py
    Then type 's' + Enter for status, 'q' + Enter to quit.
"""

import asyncio
import sys
from datetime import datetime

from tcp_model_output import TcpModelOutput, MODEL_TCP_PORT, PING_TIMEOUT as MODEL_PING_TIMEOUT
from tcp_station_output import TcpStationOutput, STATION_TCP_PORT, PING_TIMEOUT as STATION_PING_TIMEOUT

# ── Configuration ───────────────────────────────────────────────────────

# How often the train advances one station (seconds).
SHUTTLE_INTERVAL = 60

# Constant speed used while driving between stations (PWM fraction 0–1).
DRIVE_SPEED = 0.6

# Brake tuning — sent to the model on every connect.
BRAKE_DECEL = 3.0       # braking strength coefficient (MCU default: 0.88)
BRAKE_DEAD_ZONE = 0.13  # effective-zero threshold (MCU default)

# ── TCP server for model ───────────────────────────────────────────────

async def model_tcp_server(
    model: TcpModelOutput,
    restart_event: asyncio.Event,
    hall_event: asyncio.Event,
):
    """TCP server for the model controller. Handles HELLO/PING/HALL."""

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
                        print(f"[{now_str}] 🧲 HALL received from model (arrived at next station)")
                        hall_event.set()
                    elif msg == "PASS":
                        now_str = datetime.now().strftime("%H:%M:%S")
                        print(f"[{now_str}] 🧲 PASS received from model (magnet passed, not stopping)")
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


# ── TCP server for station display ──────────────────────────────────────

async def station_tcp_server(station: TcpStationOutput, restart_event: asyncio.Event):
    """TCP server for the station display. Handles HELLO/PING/RESTART."""

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


# ── Shuttle loop ─────────────────────────────────────────────────────────

async def shuttle_loop(
    model: TcpModelOutput,
    station_out: TcpStationOutput,
    hall_event: asyncio.Event,
    station_count_ref: list[int],
):
    """
    Every SHUTTLE_INTERVAL seconds (on a fixed schedule), advance the model
    train exactly one station: LOOPS:0 tells the MCU to stop at the very
    next magnet it hits, and SPEED:DRIVE_SPEED sets the moderate driving
    speed. Departures stay on a fixed cadence regardless of how quickly the
    HALL sensor confirms arrival.
    """
    loop = asyncio.get_running_loop()
    next_departure = loop.time()

    while True:
        hall_event.clear()
        model.send_loops(0)
        model.send_speed(DRIVE_SPEED)
        now_str = datetime.now().strftime("%H:%M:%S")
        print(f"\n[{now_str}] 🚀 Departing — one station at SPEED:{DRIVE_SPEED}")
        station_out.send_station("Running", "DRIVING")

        next_departure += SHUTTLE_INTERVAL
        remaining = max(0.0, next_departure - loop.time())

        try:
            await asyncio.wait_for(hall_event.wait(), timeout=remaining)
            station_count_ref[0] += 1
            now_str = datetime.now().strftime("%H:%M:%S")
            print(f"[{now_str}] 🏁 Arrived at station #{station_count_ref[0]}")
            station_out.send_station(f"Station {station_count_ref[0]}", "AT_STATION_VALID")
        except asyncio.TimeoutError:
            now_str = datetime.now().strftime("%H:%M:%S")
            print(f"[{now_str}] ⚠️  No HALL before next departure — continuing anyway")

        # Sleep out any leftover time so the next departure lands on schedule.
        remaining = next_departure - loop.time()
        if remaining > 0:
            await asyncio.sleep(remaining)


# ── Stdin listener ──────────────────────────────────────────────────────

async def stdin_listener(status_fn):
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
    """Main entry point for minute-shuttle mode."""
    model = TcpModelOutput()
    station_out = TcpStationOutput()
    restart_event = asyncio.Event()
    hall_event = asyncio.Event()
    station_count_ref = [0]

    await model_tcp_server(model, restart_event, hall_event)
    await station_tcp_server(station_out, restart_event)
    print()

    station_out.send_station("Running", "DRIVING")

    def status_fn():
        return (f"Interval: {SHUTTLE_INTERVAL}s, Speed: {DRIVE_SPEED}, "
                f"Stations passed: {station_count_ref[0]}")

    stdin_task = asyncio.create_task(stdin_listener(status_fn))
    shuttle_task = asyncio.create_task(shuttle_loop(
        model, station_out, hall_event, station_count_ref,
    ))

    try:
        await asyncio.gather(stdin_task, shuttle_task)
    except KeyboardInterrupt:
        print("\n👋 Stopped by user")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Server stopped")
        sys.exit(0)
