#!/usr/bin/env python3
"""
Simple TCP server for testing the MicroPython client.
Sends test commands and echoes received messages.

Usage:
    python simple_tcp_server.py
    
Commands (type and press Enter):
"""

import asyncio
import sys
import threading

# ============================================================
# CONFIGURATION
# ============================================================
PORT = 8080

# Watchdog timer - should match MCU PONG_TIMEOUT + buffer
PING_TIMEOUT = 15  # seconds - if no PING received, close connection
# ============================================================

client_writer = None


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    global client_writer
    peer = writer.get_extra_info("peername")
    print(f"\n✅ Client connected from {peer}")
    client_writer = writer
    
    last_ping_received = asyncio.get_event_loop().time()
    
    try:
        # Wait for HELLO:MODEL
        line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        hello = line.decode().strip()
        print(f"📩 Received: {hello}")
        
        if hello == "HELLO:MODEL":
            writer.write(b"ACK\n")
            await writer.drain()
            print("📤 Sent: ACK")
        
        print("\nCommands: p=ping, l=led_button, s=speed, r=reverser, q=quit")
        print("Type command and press Enter:\n")
        
        # Read from client in loop
        while True:
            try:
                # Check watchdog timeout
                current_time = asyncio.get_event_loop().time()
                if current_time - last_ping_received > PING_TIMEOUT:
                    print(f"\n⚠️  No PING received for {PING_TIMEOUT}s - closing connection")
                    break
                
                line = await asyncio.wait_for(reader.readline(), timeout=1.0)
                if not line:
                    print("\n⚠️  Client disconnected")
                    break
                msg = line.decode().strip()
                print(f"📩 Received: {msg}")
                
                # Handle PING from client
                if msg == "PING":
                    last_ping_received = current_time
                    writer.write(b"PONG\n")
                    await writer.drain()
                    print("📤 Sent: PONG")
                    
            except asyncio.TimeoutError:
                # No data received in 1s, continue to check watchdog
                continue
            except Exception as e:
                print(f"\n❌ Read error: {e}")
                break
            
    except asyncio.TimeoutError:
        print("❌ Timeout waiting for HELLO")
    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        client_writer = None
        writer.close()
        await writer.wait_closed()
        print("\n🔌 Connection closed")


async def _send(writer: asyncio.StreamWriter, data: bytes):
    """Send data safely from within the event loop (called via run_coroutine_threadsafe)."""
    writer.write(data)
    await writer.drain()


def input_thread():
    """Thread to read stdin and send commands"""
    commands = {
        'p': b"PONG\n",
        'l': b"LED_BUTTON\n",
        's': b"SPEED:0.0\n",
        'j': b"SPEED:0.2\n",
        'k': b"SPEED:0.35\n",
        'l': b"SPEED:0.4\n",
        'f': b"REVERSER:1\n",
        'r': b"REVERSER:0\n",
        'd': b"STATION:Hello from server!:valid\n",
    }
    
    while True:
        try:
            cmd = input().strip()
            
            if cmd == 'q':
                print("Quitting...")
                sys.exit(0)
            
            msg = commands.get(cmd) or (cmd + "\n").encode() if cmd else None
            if msg:
                if client_writer:
                    asyncio.run_coroutine_threadsafe(_send(client_writer, msg), loop)
                    print(f"📤 Sent: {msg.decode().strip()}")
                else:
                    print("⚠️  No client connected")
        except EOFError:
            break
        except Exception as e:
            print(f"Input error: {e}")


async def main():
    global loop
    loop = asyncio.get_running_loop()
    
    # Start input thread
    thread = threading.Thread(target=input_thread, daemon=True)
    thread.start()
    
    # Bind to all interfaces
    server = await asyncio.start_server(handle_client, "0.0.0.0", PORT)
    addr = server.sockets[0].getsockname()
    print(f"🚀 TCP server listening on {addr[0]}:{addr[1]}")
    print("Waiting for client connection...")
    
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Server stopped")
        sys.exit(0)
