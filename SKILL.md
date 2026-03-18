---
name: mammotion
description: Control Mammotion robotic mowers (Luba, Yuka) via cloud API. Start/stop mowing jobs, check status, manage areas, view schedules and history. Use when the user wants to control their robot lawn mower.
compatibility: Requires 'mammotion.py' script in PATH with MAMMOTION_EMAIL and MAMMOTION_PASSWORD environment variables set.
---

# Mammotion Robotic Mower Control

Control Mammotion robotic mowers via cloud API using the `mammotion.py` CLI. Supports Luba, Yuka, and other Mammotion models.

## Authentication

Credentials are loaded from environment variables:
```bash
export MAMMOTION_EMAIL="your@email.com"
export MAMMOTION_PASSWORD="yourpassword"
```

Or passed directly: `mammotion.py -e email -p password <command>`

Auth tokens are cached automatically at `~/.mammotion/auth.json` for faster subsequent calls.

## Commands Reference

### List Devices
```bash
mammotion.py devices
```
Lists all mowers and RTK base stations on the account.

### Check Status
```bash
mammotion.py status --device Luba-XXXXXX
```
Returns:
- Status (idle, mowing, charging, paused, returning)
- Battery percentage
- Progress and time remaining (when mowing)
- Position coordinates and heading
- Blade height
- RTK fix quality and satellite count
- Lifetime stats (hours, mileage)

### Start Mowing
```bash
# Basic - mow specific areas
mammotion.py start --device Luba-XXXXXX --areas front-yard back-yard

# Full options
mammotion.py start --device Luba-XXXXXX \
  --areas front-yard \
  --pattern zigzag \
  --cutting-height 2.8 \
  --path-spacing 10.0 \
  --perimeter-laps 2 \
  --mow-order grid-first \
  --mowing-angle 45 \
  --speed 0.5
```

**Start Options:**
| Option | Default | Range/Values | Description |
|--------|---------|--------------|-------------|
| `--areas` | required | area names | Space-separated area names to mow |
| `--pattern` | zigzag | zigzag, chessboard, perimeter, adaptive | Mowing path pattern |
| `--cutting-height` | 2.8 | 2.2-3.9 inches | Blade cutting height |
| `--path-spacing` | 10.0 | 7.9-13.8 inches | Distance between mowing passes |
| `--perimeter-laps` | 2 | 0-4 | Number of border passes |
| `--mow-order` | grid-first | grid-first, perimeter-first | Order of operations |
| `--mowing-angle` | 0 | 0-359 degrees | Direction of mowing lines |
| `--speed` | 0.5 | 0.0-1.0 | Mowing speed (0=slow, 1=fast) |

### Control Commands
```bash
# Pause active mowing job
mammotion.py pause --device Luba-XXXXXX

# Resume paused job
mammotion.py resume --device Luba-XXXXXX

# Return to charging dock
mammotion.py return --device Luba-XXXXXX

# Cancel current job
mammotion.py cancel --device Luba-XXXXXX
```

### List Mowing Areas
```bash
mammotion.py areas --device Luba-XXXXXX
```
Lists all defined mowing zones with their names and hashes. Areas are created in the Mammotion mobile app.

### View Schedules
```bash
mammotion.py schedules --device Luba-XXXXXX
mammotion.py schedules --device Luba-XXXXXX --verbose  # debug info
```
Shows scheduled mowing tasks with times, days, areas, and settings.

### Mowing History
```bash
mammotion.py reports --device Luba-XXXXXX
mammotion.py reports --device Luba-XXXXXX --count 20  # more reports
```
Returns mowing session history with timestamps, duration, area covered, and completion status.

## Mowing Patterns Explained

| Pattern | Description |
|---------|-------------|
| `zigzag` | Single-pass back and forth lines (efficient, default) |
| `chessboard` | Cross-hatch pattern with perpendicular passes (thorough cut) |
| `perimeter` | Border/edge only, no interior mowing |
| `adaptive` | Smart zigzag that adapts to terrain |

## RTK Base Stations

RTK devices (names starting with "RTK") have limited commands:
- `status` shows online/offline and product info
- Other commands return "RTK does not support this command"

## Example Workflows

**Quick mow the front yard:**
```bash
mammotion.py start --device Luba-XXXXXX --areas front-yard
```

**Thorough cut with cross-hatch pattern:**
```bash
mammotion.py start --device Luba-XXXXXX --areas backyard \
  --pattern chessboard --cutting-height 2.5 --speed 0.3
```

**Check if mower is available:**
```bash
mammotion.py status --device Luba-XXXXXX
# If charging with high battery, it's ready to mow
```

**Emergency stop and return:**
```bash
mammotion.py cancel --device Luba-XXXXXX
mammotion.py return --device Luba-XXXXXX
```

## Device Status Values

| Status | Meaning |
|--------|---------|
| online/idle | Ready to mow |
| mowing | Actively cutting grass |
| paused | Job paused, can resume |
| returning to dock | Heading back to charger |
| charging | On dock, charging |
| ready | Charged and ready |
| locked | Device is locked |
| location error | GPS/RTK fix issue |

## Tips

- Always check `status` before starting a job to verify battery and RTK fix
- Use `areas` command first to see available zone names for `--areas` parameter
- The `--speed` parameter affects battery consumption; lower speeds = longer runtime
- Perimeter laps clean up edges; 2 laps is a good default
- Mowing angle of 0° = east-west lines, 90° = north-south lines
- Cross-hatch different angles on alternating mows for healthier lawn
