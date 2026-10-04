---
name: mammotion
description: Control Mammotion robotic mowers via cloud API, inspect status, areas, schedules and history. Autonomous start is limited to caller-verified Luba 2 / Pro H variants; cloud actions are not emergency stops.
compatibility: Requires Python 3.14+, uv, mammotion.py, and MAMMOTION_EMAIL/MAMMOTION_PASSWORD environment variables.
---

# Mammotion Robotic Mower Control

Global skill location: `~/.pi/agent/skills/mammotion/SKILL.md`.

Use the single-file CLI with PyMammotion **0.10.7**, explicit `orjson==3.12.0` and `betterproto2==0.10.0`, with pinned direct dependencies and no required lockfile. `packaging==26.3` is explicit because PyMammotion imports it without declaring it.

## Safety and capability boundaries

- The CLI **cannot guarantee safety**. In an emergency, instruct the user to use the mower's **physical STOP button**. Never describe cloud pause, cancel, or return as an emergency stop. Return may cause movement.
- Obtain the user's authorization before starting, resuming, or otherwise moving a mower. Check fresh status and the physical environment; battery, online/READY, or charging state alone does not establish safety.
- Autonomous start assumes **Luba 2 / Luba Pro H hardware: 55–100 mm in 5 mm increments**. The CLI rejects Yuka, Luba 1, and names failing `DeviceType.is_luba_pro`, but cannot identify H hardware from a name. **Ask the caller to verify the H variant and height range**; do not treat the name gate as automatic H detection.
- **Luba 1:** `areas` and `start` fail clearly because the area-name shortcut is unsupported. Do not invent a generic route or full-map fallback.
- **Yuka:** start is unsupported until model-specific route and height validation. Listing/status support does not imply autonomous start support.
- **RTK:** listing and metadata-only status (cloud online/offline and product info), not mower telemetry or mower actions.
- Actions use `Priority.USER` and wait for state confirmation. A send/acknowledgement alone does not prove execution. An unconfirmed outcome may still have moved the mower: fetch fresh status before retrying; do not blindly repeat a start.

## Execution and authentication

Prefer an explicit script path. For the standalone installation:

```bash
uv run --script ~/.local/bin/mammotion.py devices
```

Every example below assumes the repository working directory; replace `mammotion.py` with the installed path as needed. No adjacent lockfile is required.

For deliberate dependency maintenance, edit the exact inline pins and test a refreshed environment:

```bash
uv run --refresh --script mammotion.py --help
```

Exact inline pins do not upgrade themselves. Transitive versions may change on fresh resolution. Copy the script when updating a standalone installation.

Credentials come from `MAMMOTION_EMAIL` and `MAMMOTION_PASSWORD`, or global `-e EMAIL -p PASSWORD` arguments. Prefer environment variables; never expose passwords or the sensitive `~/.mammotion.json` cache in logs/responses.

The cache is saved via the credentials-update callback and on shutdown. Network failure preserves it; MQTT timeout does not force a fresh login. Authentication rejection is distinct from connection failure. Do not delete the cache or repeatedly log in as a generic connectivity remedy.

Mobile apps and other integrations share cloud account/session resources. Concurrent clients may contend for sessions, interrupt MQTT or change state between checks. Avoid competing control and repeated logins. Target-device readiness, not merely account login or another device's connection, is required.

## Command reference

### Inspect before acting

```bash
uv run --script mammotion.py devices
uv run --script mammotion.py status --device Luba-XXXXXX
uv run --script mammotion.py areas --device Luba-XXXXXX
```

Mower status requests fresh telemetry: state, battery, docked indicator, progress, position, height and RTK information where available. A timeout is not valid fresh status. Zone names/hashes come from the mower; zones are created in the mobile app.

### Start (caller-verified H variant only)

```bash
uv run --script mammotion.py start --device Luba-XXXXXX \
  --areas front-yard --pattern chessboard --cutting-height 2.5 \
  --path-spacing 10.0 --perimeter-laps 2 --mow-order grid-first \
  --mowing-angle 45 --speed 0.25
```

| Option | Default | Range/values |
|--------|---------|--------------|
| `--areas` | required | Space-separated names; quote individual names containing spaces |
| `--pattern` | zigzag | zigzag, chessboard, perimeter, adaptive |
| `--cutting-height` | 2.5 | 2.2–3.9 inches, converted to 5 mm steps within 55–100 mm |
| `--path-spacing` | 10.0 | 7.9–13.8 inches |
| `--perimeter-laps` | 2 | 0–4 |
| `--mow-order` | grid-first | grid-first, perimeter-first |
| `--mowing-angle` | 0 | 0–359 degrees (base direction) |
| `--speed` | 0.25 | 0.0–1.0 normalized speed |

Zigzag is a single-pass line pattern; chessboard uses perpendicular passes with **fixed 90° included angle**. Perimeter is border-only; adaptive is the upstream adaptive pattern. Route configuration uses upstream `OperationSettings` / `build_route_information` for model flags; do not improvise route bytes or manual blade commands.

### Ordinary remote actions

```bash
uv run --script mammotion.py pause --device Luba-XXXXXX
uv run --script mammotion.py resume --device Luba-XXXXXX
uv run --script mammotion.py return --device Luba-XXXXXX
uv run --script mammotion.py cancel --device Luba-XXXXXX
```

These check state preconditions and confirm the resulting state. Cloud connectivity or confirmation can fail; none is a safety-rated stop.

### Schedules and history

```bash
uv run --script mammotion.py schedule --device Luba-XXXXXX
uv run --script mammotion.py reports --device Luba-XXXXXX --count 20
```

Schedules list task settings; reports show session history. Neither establishes current mower readiness.

## Interpreting state

`mowing` means active work; `paused` means a resumable job; `returning` means movement toward the dock. `charging` and `ready` do not alone prove safe operation. Use the explicit docked indicator instead of inferring docking from the state name. Locked, offline, location-error, or unconfirmed telemetry requires investigation, not an automatic retry.
