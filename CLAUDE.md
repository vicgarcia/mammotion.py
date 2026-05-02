# CLAUDE.md — mammotion.py implementation notes

## Architecture

Single-file PEP 723 script (`uv run --script`). Requires **Python 3.14** (specified in script header). All logic lives in `mammotion.py`. The main class is `MammotionCLI`, which wraps the upstream `PyMammotionClient` from `pymammotion.client`.

### PyMammotion library version

`pymammotion>=0.7.90` — this was a **major architectural rewrite** from the 0.5.x/0.7.0 era. The old `MammotionBaseCloudDevice`, `MammotionCloud`, and `AliyunMQTT` classes are gone. Do not attempt to use them.

### Communication layer

All device commands go through the persistent `PyMammotionClient` instance:

```python
await self._client.send_command_with_args(device_name, "command_name", **kwargs)
```

The client manages MQTT sessions internally — there is no manual connect/disconnect per command. The library maintains persistent Aliyun MQTT (pre-2025 devices) and/or Mammotion MQTT (post-2025 devices) connections in the background.

For request/response commands (where you need to wait for a reply):
```python
result = await self._client.send_command_and_wait(device_name, "command_name", "expected_proto_field")
```

### Session helpers

- `get_device_state(device_name)` — sends sync+report commands, polls `_client.get_device_by_name()` until sys_status is non-zero, returns state dict.
- `get_area_list(device_name)` — calls `send_command_and_wait(device, "get_area_name_list", "toapp_all_hash_name", device_id=iot_id)`. Do NOT use `start_map_sync` for this — it's a 5-minute saga that fetches the entire map and never exits cleanly from a CLI context.
- `_run_command_with_state_check(args, can_run_fn, cmd, ...)` — gets device state, checks precondition, sends command. Used by pause/resume/return/cancel.

### Connection readiness

After `login()` / `restore_credentials()`, the MQTT transport task is started but the connection handshake hasn't completed yet. Sending commands immediately causes a race condition (first run times out, second run works).

`_wait_for_connection(timeout=12.0)` polls `session.aliyun_transport.is_connected` (or `mammotion_transport`) every 250ms until True. It is called in `run()` after login and before the first command. Do not remove this — without it, first-run commands reliably fail.

### Auth cache

- Cache file: `~/.mammotion.json`
- On login: `_client.to_cache()` serializes credentials, saved to file.
- On startup: `_client.restore_credentials(email, password, cache_dict)` restores session without re-authenticating.
- If restore fails, falls back to fresh `login_and_initiate_cloud(email, password)`.

### Device registry

After login, devices are registered internally by `PyMammotionClient`:
- `_client.aliyun_device_list` — pre-2025 devices (Aliyun MQTT)
- `_client.mammotion_device_list` — post-2025 devices (Mammotion MQTT)
- `_client.get_device_by_name(name)` — returns `MowerDevice` state object
- `_client.mower(name)` — returns `DeviceHandle`

## Commands to avoid

- **`start_map_sync()`** — launches a full `MapFetchSaga` with a 5-minute timeout that runs as a background task. The CLI never exits cleanly. Use `send_command_and_wait(..., "get_area_name_list", "toapp_all_hash_name")` instead.
- **`send_todev_ble_sync(sync_type=3)`** — a BLE radio sync command in the device firmware. Has nothing to do with cloud MQTT availability. Do not use as a "warmup".

## Key implementation details

### Blade motor activation (Luba 2 / Luba Pro)

Blade motor activation during autonomous mowing is **controlled entirely by the device firmware**. No explicit blade control command should be sent.

The `path_order` byte string passed in `GenerateRouteInformation` contains mode flags:
- **`path_order_bytes[5] = 8`** for Luba 2 / Luba Pro (`DeviceType.is_luba_pro()` returns True)
- **`path_order_bytes[5] = 0`** for Luba 1

Without `bArr[5] = 8`, the device navigates the route geometry but does not engage the cutting motor. This is the `reserved` field in `NavReqCoverPath` proto.

Do **not** add `set_blade_control(on_off=1)` or `operate_on_device(...)` after `start_job` — these are manual/joystick commands that have no effect during autonomous route execution.

### Blade height

- Luba 2 "H" models: 55–100mm in 5mm increments (2.2–3.9 inches)
- Conversion: `round(inches * 25.4 / 5) * 5`, clamped to [55, 100]
- Sent as `blade_height` in `GenerateRouteInformation` → maps to `knife_height` in `NavReqCoverPath`
- The device physically adjusts blade height when it arrives at the mowing zone.

### Device detection

- `DeviceType.is_luba_pro(device_name)` — True for Luba 2 and higher
- Our Luba 2 device: `Luba-VSAMK6N4`

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

1. `get_area_list()` — syncs map via `start_map_sync` saga, gets area names/hashes
2. `start_mow_path_saga(device, area_hashes, route_info)` — plans the route
3. `send_command_with_args(device, "start_job")` — begins execution
4. Device runs autonomously from this point

### Command flow for pause/resume/return/cancel

1. `get_device_state()` — get current sys_status via MQTT
2. Check preconditions (is device in the right state?)
3. `send_command_with_args(device, cmd_name)` — send the command

### Known command names (passed as strings to send_command_with_args)

- `send_todev_ble_sync` (sync_type=3) — sync device state
- `get_report_cfg` — request current status report
- `start_job` — `NavTaskCtrl(type=1, action=1)`
- `cancel_job` — `NavTaskCtrl(type=1, action=4)`
- `return_to_dock` — `NavTaskCtrl(type=1, action=5)`
- `pause_execute_task` — `NavTaskCtrl(type=1, action=2)`
- `read_plan` (sub_cmd=2, plan_index=N) — fetch scheduled tasks
- `get_area_name_list` — area names
- `get_all_boundary_hash_list` (sub_cmd=0) — boundary hashes
- `query_job_history` — check if history available
- `request_job_history` (num=N) — fetch N work reports

### IDE import warning

The IDE warns that `pymammotion.client` can't be resolved. This is expected — the package is installed at runtime by `uv run --script`, not in the local environment. The warning is harmless.
