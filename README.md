# mammotion.py

Single-file Python CLI for Mammotion cloud devices, built on [PyMammotion](https://github.com/mikey0000/PyMammotion) **0.10.7**.

## Requirements and installation

- Python 3.14+, [uv](https://github.com/astral-sh/uv), and a Mammotion account with registered devices.
- Inline dependencies: `pymammotion==0.10.7`, `orjson==3.12.0`, `betterproto2==0.10.0`. Also `packaging==26.3`: PyMammotion imports it but omits it from its dependency metadata.
- `mammotion.py.lock` locks the script environment, including transitive dependencies.

```bash
git clone https://github.com/vicgarcia/mammotion.py.git
cd mammotion.py
uv run --locked --script mammotion.py --help
```

Maintainers regenerate the script lock after dependency changes with:

```bash
uv lock --script mammotion.py --upgrade
```

Review and commit both the inline dependency changes and `mammotion.py.lock`. Exact pins remain fixed until explicitly edited; `--upgrade` re-resolves within those constraints.

For a standalone installation, copy **both files** together:

```bash
mkdir -p ~/.local/bin
cp mammotion.py mammotion.py.lock ~/.local/bin/
uv run --locked --script ~/.local/bin/mammotion.py --help
```

The installed script needs its adjacent `mammotion.py.lock`. Its executable shebang also enforces `--locked`; run `chmod +x ~/.local/bin/mammotion.py` to invoke it directly as `mammotion.py`.

## Safety and model limits

**This CLI cannot guarantee safety. Use the mower's physical STOP button in an emergency.** Cloud `pause`, `cancel`, and `return` are ordinary remote actions, not emergency stops; return can cause movement. Network delay, session contention, and stale telemetry can prevent or delay execution.

- Autonomous `start` is intended only for **Luba 2 / Luba Pro H models** using the documented **55–100 mm, 5 mm increment** height assumptions. The CLI rejects Yuka, Luba 1, and names not accepted by `DeviceType.is_luba_pro`, but **cannot identify H hardware from the name**. The caller must verify the H variant and height range before starting. Passing the name gate is not hardware validation or generic route support.
- **Luba 1:** `areas` and `start` fail clearly because the area-name shortcut is unsupported; this CLI does not fetch a full map as a fallback.
- **Yuka:** autonomous start is unsupported until model-specific route and height validation is completed. Device listing or status support does not imply start support.
- **RTK base stations:** listing and metadata-only `status` (cloud online/offline and product information); no mower actions or live mower telemetry.

Before starting, inspect fresh status, area names, battery, positioning, and the physical surroundings. Charging, READY, or online status alone is not proof that operation is safe.

## Authentication and cloud sessions

```bash
export MAMMOTION_EMAIL="you@example.com"
export MAMMOTION_PASSWORD="yourpass"
```

Alternatively pass `-e EMAIL -p PASSWORD` before the command (arguments may be visible in process listings/history). Treat `~/.mammotion.json` as sensitive: it stores reusable session credentials. Credential-update callbacks persist refreshed credentials, and shutdown saves the latest cache.

Cached sessions are restored where possible. Authentication rejection may require a fresh login, but a network failure preserves the cache. An MQTT readiness timeout does **not** trigger a forced login or imply expired credentials.

The CLI and mobile app/other integrations share cloud account/session resources. Concurrent clients can contend for sessions, disrupt MQTT, or change device state between checks. Avoid concurrent control and repeated logins; a successful login does not prove the target mower is connected.

## Commands

All examples use locked execution:

```bash
uv run --locked --script mammotion.py devices
uv run --locked --script mammotion.py status --device Luba-ABC123
uv run --locked --script mammotion.py areas --device Luba-ABC123
uv run --locked --script mammotion.py schedule --device Luba-ABC123
uv run --locked --script mammotion.py reports --device Luba-ABC123 --count 20
```

| Command | Description |
|---------|-------------|
| `devices` | List account devices, including RTK stations |
| `status` | Request fresh mower status; RTK metadata only |
| `areas` | List zone names/hashes on supported models; created in the mobile app |
| `start` | Configure and start mowing on validated Luba 2 / Pro H models only |
| `pause` | Pause a running mowing job |
| `resume` | Resume a paused job |
| `return` | Request return to charging dock |
| `cancel` | Request cancellation of the current job |
| `schedule` | List scheduled tasks |
| `reports` | Show mowing history |

Commands requiring mower communication wait for **the selected device's transport**, not an unrelated device/account connection. Actions use `Priority.USER` and wait for fresh state confirmation. A send or acknowledgement alone is not success; a timeout means the outcome is unconfirmed, not necessarily that nothing happened. Check fresh status before considering a retry.

### Start options

Use these examples only after verifying the selected device is a supported H model:

```bash
uv run --locked --script mammotion.py start --device Luba-ABC123 --areas front-yard back-yard
uv run --locked --script mammotion.py start --device Luba-ABC123 --areas front-yard \
  --cutting-height 2.5 --speed 0.7 --perimeter-laps 2 \
  --mow-order perimeter-first --pattern chessboard
```

| Option | Default | Description |
|--------|---------|-------------|
| `--areas` | required | Space-separated area names; quote individual names containing spaces |
| `--cutting-height` | 2.5 | Inches (2.2–3.9); rounded to 5 mm increments and clamped to 55–100 mm |
| `--speed` | 0.25 | Normalized speed, 0.0–1.0 |
| `--perimeter-laps` | 2 | Border laps, 0–4 |
| `--mow-order` | grid-first | `grid-first` or `perimeter-first` |
| `--pattern` | zigzag | `zigzag`, `chessboard`, `perimeter`, or `adaptive` |
| `--path-spacing` | 10.0 | Inches, 7.9–13.8 |
| `--mowing-angle` | 0 | Base mowing angle, 0–359 degrees |

Route configuration uses upstream `OperationSettings` / `build_route_information` for model-specific path flags. Chessboard uses a fixed **90° included angle** between passes; `--mowing-angle` sets the base direction, not that included angle.

### Remote actions (not emergency stops)

```bash
uv run --locked --script mammotion.py pause --device Luba-ABC123
uv run --locked --script mammotion.py resume --device Luba-ABC123
uv run --locked --script mammotion.py return --device Luba-ABC123
uv run --locked --script mammotion.py cancel --device Luba-ABC123
```

## Offline regression tests

```bash
uv run --locked --script mammotion.py --help
uv run --no-project --python 3.14 --with pymammotion==0.10.7 --with orjson==3.12.0 --with betterproto2==0.10.0 --with packaging==26.3 \
  python -m unittest discover -s tests -v
```

Tests use mocks and do not log in or send live mower commands. Real device behavior still requires separately authorized validation.

## Agent skill

Install the project skill at the global pi location:

```bash
mkdir -p ~/.pi/agent/skills/mammotion
cp SKILL.md ~/.pi/agent/skills/mammotion/SKILL.md
```

Keep the installed script and adjacent lockfile together, and set credentials in the agent's environment. The skill describes model restrictions, locked command execution, status confirmation, and physical-stop safety limits.
