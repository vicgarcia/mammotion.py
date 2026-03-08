# mammotion.py - Mammotion Mower Control CLI

A python CLI tool for controlling your Mammotion robotic automower built using the [PyMammotion](https://github.com/mikey0000/PyMammotion) library

## Features

- **Single-file executable** - Uses `uv run --script` with inline dependencies (PEP 723)
- **Cloud control** - Connect to your mower from anywhere via Mammotion cloud API
- **Start mowing tasks** - Specify areas, patterns, cutting height, speed, and more
- **Device management** - List devices, check status, view areas and schedules
- **RTK support** - Shows RTK base station status
- **Environment variables** - Set credentials via `MAMMOTION_EMAIL` and `MAMMOTION_PASSWORD`

## Prerequisites

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) package manager
- A Mammotion account with registered devices

## Installation

```bash
# Install uv (if needed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and run
git clone <repo>
cd mammotion.py
chmod +x mammotion.py

# Run directly - uv auto-installs dependencies
./mammotion.py --help
```

## Usage

Set credentials via environment variables:
```bash
export MAMMOTION_EMAIL="you@example.com"
export MAMMOTION_PASSWORD="yourpass"
```

Or pass them as arguments: `-e you@example.com -p yourpass`

### List devices
```bash
./mammotion.py devices
```

### Check status
```bash
./mammotion.py status --device Luba-ABC123
```

### Start mowing task
```bash
# Mow specific areas with defaults
./mammotion.py start --device Luba-ABC123 --areas front-yard back-yard

# With custom settings
./mammotion.py start --device Luba-ABC123 --areas front-yard \
  --cutting-height 2.5 \
  --speed 0.7 \
  --perimeter-laps 2 \
  --mow-order perimeter-first \
  --pattern zigzag
```

### Control commands
```bash
./mammotion.py pause --device Luba-ABC123
./mammotion.py resume --device Luba-ABC123
./mammotion.py return --device Luba-ABC123
./mammotion.py cancel --device Luba-ABC123
```

### View areas and schedules
```bash
./mammotion.py areas --device Luba-ABC123
./mammotion.py schedules --device Luba-ABC123
./mammotion.py reports --device Luba-ABC123
```

## Commands

| Command | Description |
|---------|-------------|
| `devices` | List all devices on your account |
| `status` | Show device status (battery, position, RTK, etc.) |
| `start` | Start a mowing task with specified areas |
| `pause` | Pause current mowing job |
| `resume` | Resume paused job |
| `return` | Return to charging dock |
| `cancel` | Cancel current job |
| `areas` | List all mowing areas/zones |
| `schedules` | List scheduled mowing tasks |
| `reports` | Show mowing job history |

## Start Command Options

| Option | Default | Description |
|--------|---------|-------------|
| `--areas` | required | Space-separated area names to mow |
| `--cutting-height` | 2.8 | Cutting height in inches (2.2-3.9) |
| `--speed` | 0.5 | Mowing speed: 0.0 (slow) to 1.0 (fast) |
| `--perimeter-laps` | 2 | Number of border laps (0-4) |
| `--mow-order` | grid-first | `grid-first` or `perimeter-first` |
| `--pattern` | zigzag | `zigzag`, `chessboard`, `perimeter`, or `adaptive` |
| `--path-spacing` | 10.0 | Path spacing in inches (7.9-13.8) |
| `--mowing-angle` | 0 | Mowing angle in degrees (0-359) |

## RTK Base Stations

RTK base stations are detected automatically. The `status` command shows:
- Online/offline status
- Product info

Other commands return "RTK does not support this command".
