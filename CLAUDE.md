# CLAUDE.md — mammotion.py implementation notes

## Architecture

Single-file PEP 723 script (`uv run --script`). All logic lives in `mammotion.py`. The main class is `MammotionCLI`.

### Communication layer

All device commands use **pure MQTT** via `MammotionBaseCloudDevice.queue_command(name, **kwargs)`. This dispatches through `getattr(self._commands, key)(**kwargs)` where `self._commands` is a `MammotionCommand` instance.

**Do not use `self.http.mqtt_invoke()`** for device commands — it is an HTTP relay that rejects most commands with "Invalid device".

### Session helpers

- `_open_mqtt_session(device_name)` — connects MQTT, syncs, waits for device state, returns `(state_dict, cloud_device, mqtt)`. Caller must call `mqtt.disconnect()` when done.
- `_run_mqtt_command(args, can_run_fn, mqtt_cmd, ...)` — opens a session, checks device state, sends a single command. Used by pause/resume/return/cancel.

## Key implementation details

### Blade motor activation (Luba 2 / Luba Pro)

Blade motor activation during autonomous mowing is **controlled entirely by the device firmware** — the device starts blades automatically when it arrives at the mowing zone. No explicit blade control command should be sent from the app side.

The `path_order` byte string passed in `generate_route_information` contains mode flags:
- **`path_order_bytes[5] = 8`** for Luba 2 / Luba Pro (`DeviceType.is_luba_pro()` returns True)
- **`path_order_bytes[5] = 0`** for Luba 1

Without `bArr[5] = 8`, the device navigates the route geometry but does not engage the cutting motor. This is the `reserved` field in `NavReqCoverPath` proto.

Do **not** add `set_blade_control(on_off=1)` or `operate_on_device(...)` calls after `start_job` — these are manual/joystick control commands that have no effect during autonomous route execution.

### Blade height

- Luba 2 "H" models: 55–100mm in 5mm increments (2.2–3.9 inches)
- Conversion: `round(inches * 25.4 / 5) * 5`, clamped to [55, 100]
- Sent as `blade_height` in `GenerateRouteInformation` → maps to `knife_height` in `NavReqCoverPath`
- The device physically adjusts blade height when it arrives at the mowing zone — do not pre-set it with `set_blade_height` before starting the task

### Device detection

- `DeviceType.is_luba1(device_name)` — True only for Luba 1 (`device_name[:7] == "Luba-VS"` is NOT Luba 1; Luba 1 is "Luba-VS" prefix... actually check the library source)
- `DeviceType.is_luba_pro(device_name)` — True for Luba 2 and higher (value >= LUBA_2)
- Our Luba 2 test device: `Luba-VSAMK6N4`

### Auth cache

- Cache file: `~/.mammotion.json`
- `expires_in` field stores an **absolute Unix timestamp** (not a duration)
- On stale cache: `get_devices()` returns None → auto-retry with fresh login in `run()`

### Device status codes (sys_status)

| Value | Name | Meaning |
|-------|------|---------|
| 11 | READY | Charged and ready |
| 13 | WORKING | Actively mowing |
| 14 | RETURNING | Returning to dock |
| 15 | CHARGING | On dock, charging |
| 19 | PAUSE | Job paused |
| 20 | MANUAL_MOWING | Manual/joystick control |

### Command flow for `start`

1. `generate_route_information(route_info)` — plans the route with `blade_height` and `path_order` (including the mode flags)
2. `start_job()` — sends `NavTaskCtrl(type=1, action=1)` to begin execution
3. Disconnect — the device runs autonomously from this point

### Command flow for pause/resume/return/cancel

1. `_open_mqtt_session()` — get current device state
2. Check preconditions (is device in the right state to accept this command?)
3. `queue_command(mqtt_cmd)` — send the command
4. Disconnect

## pymammotion library notes

- `set_blade_control(on_off)` — sends `SysKnifeControl(knife_status=on_off)` — system-level on/off, no height
- `operate_on_device(...)` — sends `DrvMowCtrlByHand` — manual/joystick mow control, ignored during autonomous tasks
- `set_blade_height(height)` — sends `DrvKnifeHeight` via driver channel — physically positions blade deck
- `start_job()` — `NavTaskCtrl(type=1, action=1)`
- `cancel_job()` — `NavTaskCtrl(type=1, action=4)`
- `return_to_dock()` — `NavTaskCtrl(type=1, action=5)`
- `pause_execute_task()` — `NavTaskCtrl(type=1, action=2)`
- `resume_execute_task()` — `NavTaskCtrl(type=1, action=3)` (our `cmd_resume` uses `start_job` action=1 which also works)
