# BahnTracker — Automated Model Train synchronized to real S-Bahn

A model train layout that mirrors the next real incoming train to **Fasanenpark** station — the S-Bahn stop at Infineon's Campeon campus near Munich. The model automatically drives and stops in sync with the real train, so you know when to start walking to catch your ride home.

Live train data is fetched from the [geops.io](https://geops.io) API (the same backend used by the official S-Bahn Munich Live Map). A central server processes the data and controls two PSoC 6 microcontrollers over TCP: one inside the model train, one at the station display.

---

## How It Works

```
geops.io API
     │  WebSocket
     ▼
 Raspberry Pi  ──TCP:8080──►  PSoC 6 AI Eval Kit   (model train)
 (sbahn.py)    ──TCP:8081──►  PSoC 6 WiFi BT Kit    (station display)
```

The server tracks the next S3 train heading toward the city center. It resolves GPS coordinates and timetable data into commands (speed, loops, station name, ETA) that are sent to the microcontrollers over a plain-text TCP protocol.

Two operating modes are supported:

### Active Mode (`sbahn.py`)
A 6-state Moore state machine keeps the model train in sync with the real train. The model drives in circles between stations, stopping only when the real train boards. The station display shows the current station name and ETA.

```
                   ┌──────────────────────────────────────────────────────────────┐
                   │                                                              │
            BOARDING│                                              HALL           │
WAITING_AT_NONAME ──►  AT_STATION_VALID ──DRIVING──► DRIVING ──────────► AT_STATION_VALID
       ▲                                (last stop)      │   [boarding]         │
       │                                    │            │                      │
      HALL                                  ▼           BOARDING                │
       │                          DRIVING_TO_NONAME      │                      │
       └──────────────────────────────────◄─┘            ▼                      │
                                                  RUNNING_TO_STATION ───────────┘
```

### Passive Mode (`magnet_station_server.py`)
The model progresses between 5 physical magnet positions on the track, each representing a real station (Deisenhofen → Fasanenpark). No state machine — the server just sends `LOOPS:X` to advance to the next station on each boarding event. Lower noise, suitable for an office setting.

---

## Hardware

| Component | Role |
|---|---|
| PSoC 6 AI Evaluation Kit (CY8CKIT-062S2-AI) | Model train controller (onboard) |
| PSoC 6 WiFi BT Prototyping Kit (CY8CPROTO-062-4343W) | Station display controller |
| H-Bridge KIT 2GO | Motor driver for the model train |
| TLE4964-3M Hall sensor | Detects track magnets to determine train position |
| I²C LCD 1602 module | Station name + ETA display |
| Raspberry Pi (any) | Runs the Python server |
| H0-gauge track + rolling stock | 160 cm × 80 cm plywood layout |
| Power bank | Keeps the onboard PSoC 6 powered across rail contact gaps |

The model train is powered through the rails (12 V DC), while the onboard microcontroller runs off a power bank to avoid resets from intermittent rail contact.

---

## Repository Structure

```
sbahn.py                    # Active mode server (state machine)
magnet_station_server.py    # Passive mode server (magnet stations)
train_state_machine.py      # 6-state Moore machine
tcp_model_output.py         # TCP server for model train MCU (port 8080)
tcp_station_output.py       # TCP server for station display MCU (port 8081)
outputs.py                  # Abstract output interfaces
travel_times.json           # Station list and inter-station travel times

micropython/
  model_controller.py       # Flash to model train PSoC 6
  station_controller.py     # Flash to station display PSoC 6
  mp_i2c_lcd1602.py         # I²C LCD driver
  wifi.py                   # WiFi connection helper
  wifi_config.json          # WiFi credentials (edit before flashing)
  server_config.json        # Server IP + port (edit before flashing)
```

---

## Setup

### 1. Server

Clone the repository on any device on your local network (we use a Raspberry Pi 4):

```bash
git clone <repo-url>
cd bahntracker
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Run the active mode server:

```bash
python sbahn.py
```

Or the passive mode server:

```bash
python magnet_station_server.py
```

> **Tip:** Assign a static IP address to the server in your router so the MCU config never needs updating.

Interactive commands while the server is running:
- `h` + Enter — simulate a Hall sensor trigger (active mode)
- `s` + Enter — print current status
- `q` + Enter — quit

### 2. MicroPython on the PSoC 6

Follow the [MicroPython for PSoC 6 guide](https://micropython.org) to flash MicroPython onto both boards.

Edit the config files before copying:

**`micropython/wifi_config.json`**
```json
{"ssid": "YourNetwork", "password": "YourPassword"}
```

**`micropython/server_config.json`**
```json
{"host": "192.168.x.x", "port": 8080}
```

Copy to the **model train controller** (port 8080):
- `model_controller.py` → rename to `main.py` on the device
- `wifi.py`
- `wifi_config.json`
- `server_config.json`

Copy to the **station display controller** (port 8081):
- `station_controller.py` → rename to `main.py` on the device
- `mp_i2c_lcd1602.py`
- `wifi.py`
- `wifi_config.json`
- `server_config.json` (set port to `8081`)

---

## Communication Protocol

All messages are newline-terminated (`\n`). One command per line.

**Server → Model Train Controller**
```
SPEED:0.75          # set speed [0.0–1.0]
LOOPS:2             # pass over magnet N extra times before stopping; negative = infinite
REVERSER:1          # travel direction (1 = forward)
PONG                # reply to PING
```

**Server → Station Display**
```
STATION:Taufkirchen:DRIVING    # station name + state machine state
ETA:1743350400                 # unix timestamp of arrival, or ETA:none
ACK                            # connection acknowledged
PONG
```

**Model Train Controller → Server**
```
PING
HALL                # magnet detected under train
```

**Station Display → Server**
```
PING
RESTART             # button press — re-select train to track
```

---

## Adapting to Your Own Network

The server code is intentionally decoupled from the API layer. To track trains on a different network or API:

1. Replace the WebSocket logic in `sbahn.py` with your data source.
2. Call `state_machine.on_api_state_change("BOARDING"/"DRIVING", ...)` and `state_machine.on_hall_sensor()` as events arrive.
3. The MCU firmware and TCP protocol remain unchanged.

---

## License

MIT
