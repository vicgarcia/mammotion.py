# CLAUDE.md — mammotion.py implementation notes

## Runtime and dependency contract

All CLI logic lives in the single-file PEP 723 script `mammotion.py`; Python **3.14+** is required. `MammotionCLI` wraps upstream `MammotionClient` from `pymammotion.client`.

Inline dependencies are exact: **`pymammotion==0.10.7`**, **`orjson==3.12.0`**, **`betterproto2==0.10.0`**. **`packaging==26.3`** is also required explicitly: PyMammotion imports `packaging.version` in `transport/cloud.py` but omits it from its published dependencies. A clean environment fails without it. Private API assumptions below are tied to this pinned upstream version; review them on upgrades.

Run the script without a lockfile:

```bash
uv run --script mammotion.py --help
```

For upgrades, explicitly edit the exact inline pins and run `uv run --refresh --script mammotion.py --help`, followed by offline tests. Direct pins remain fixed; transitive versions may change on fresh resolution. A standalone installation needs only `mammotion.py`, invoked through its uv shebang or `uv run --script /path/to/mammotion.py ...`.

Global pi skill: `~/.pi/agent/skills/mammotion/SKILL.md`.

## Upstream 0.10.7 facts

The client manages cloud sessions, device handles, brokers, command queues, reducers and MQTT transports. Do not revive the old `MammotionBaseCloudDevice` / `MammotionCloud` / `AliyunMQTT` architecture or manually connect/disconnect for each command.

### HTTP header

`MammotionClient(ha_version="0.5.7")` retains the CLI's integration header configuration. In 0.10.7, `MammotionHTTP` formats `App-Version` as `HA,2.{ha_version}` when supplied, and **`NOT HA,{APP_VERSION}`** otherwise. The old claim that omitting `ha_version` produces `ALIYUN DEMO,...` and necessarily causes a 403 is outdated; that value is a commented-out legacy alternative, not the current default.

### Commands and priority

```python
from pymammotion.messaging.command_queue import Priority

await client.send_command_with_args(
    device_name, "pause_execute_task", priority=Priority.USER
)
result = await client.send_command_and_wait(
    device_name, "generate_route_information", "bidire_reqconver_path",
    priority=Priority.USER, **route_kwargs
)
```

`send_command_with_args` defaults to `Priority.NORMAL` (queued). For genuine user actions, `Priority.USER` dispatches on the caller's task, bypassing queue delays and self-imposed quotas. It does **not** bypass a cloud 429 ban or create a missing transport. `send_command_and_wait` already uses the broker directly; USER exempts its advisory quota rather than bypassing a queue. Do not mark routine background polling as USER indiscriminately.

Transport completion or a matching response is not proof the requested action completed. Start/pause/resume/return/cancel require fresh state confirmation and explicit failure/unconfirmed reporting on timeout. Do not translate a cloud priority enum into an emergency-stop guarantee.

## Readiness, freshness and session persistence

- Login/credential restoration can finish before MQTT is usable. Wait for the **selected device's usable transport**; an unrelated device or account connection is insufficient. Metadata-only device listing and RTK status do not require mower telemetry.
- An MQTT readiness timeout is a connection failure, **not** a reason to force login. Preserve cached credentials on transient/network failure; distinguish actual authentication rejection from connectivity errors.
- `get_device_state()` requests fresh telemetry through `client.refresh_status` and a temporary pinned **`handle._reducer.apply`** hook. Only newly applied `toapp_report_data` packets containing `dev` can satisfy status predicates. RTK/work-only packets retain old status and must not count; unchanged fresh dev reports must count even when no state-change event fires. Restore the hook in `finally`. Review this private API on upgrades.
- Cache: `~/.mammotion.json`, sensitive reusable credentials. Register the async `client.on_credentials_updated` callback to serialize `client.to_cache()` after refreshes; save the latest credentials again on shutdown after successful login, before closing the client. During restoration, merge rotated login tokens into the original cache so an early callback cannot discard not-yet-restored Aliyun/device blocks. Do not discard a good cache just because network restoration failed.
- Mobile apps and other integrations share cloud account/session resources. Concurrent clients may contend for sessions or alter device state between checks. Avoid forced login loops and never infer target readiness from successful authentication.

Use current registry/handle APIs (`get_device_by_name`, `mower`) rather than obsolete device-list assumptions. `device.report_data.dev.charge_state` is separate from `sys_status`: expose its nonzero value as `docked`. A mower may report READY while still docked; status names alone are not docking or safety checks.

## Capability boundaries

- **RTK:** device listing and metadata-only status (cloud online/offline, product details), not mower actions or fresh mower reports.
- **Luba 1:** `areas` / `start` must fail clearly. The area-name shortcut is unsupported; do not silently launch a full map sync as a fallback.
- **Yuka:** reject autonomous start until model-specific route and height validation. Upstream model recognition does not prove this CLI supports its route semantics.
- Start rejects Yuka, Luba 1, and names failing `DeviceType.is_luba_pro`. That upstream helper is a broad classifier, **not H-variant detection** or a generic support guarantee.
- This CLI's autonomous start assumes **Luba 2 / Luba Pro H variants with 55–100 mm height, 5 mm steps**. Names cannot establish H hardware. **The caller must verify the H variant and applicable range before starting.** Do not claim automatic H detection or generic support for other Luba/Yuka models.

## Route construction and start

1. Verify supported family and caller-verified H hardware, target readiness, fresh status and start preconditions.
2. Fetch area names/hashes with `get_area_name_list` / `toapp_all_hash_name` on supported models; resolve requested areas explicitly.
3. Construct upstream **`OperationSettings`** and use **`build_route_information`** for route configuration and correct model-specific path flags. Avoid hand-crafted route byte offsets.
4. Send `generate_route_information` and wait for `bidire_reqconver_path`, using `Priority.USER`.
5. Send `start_job` with `Priority.USER` and confirm a newly reported working state. An acknowledgement alone is not successful mowing.

Height conversion remains `round(inches * 25.4 / 5) * 5`, clamped to [55, 100] mm under the H assumption. Chessboard's **included angle is fixed to 90°**; the user-supplied mowing angle is the base direction. Firmware controls autonomous blade activation. Do not append manual `set_blade_control` or joystick commands as an autonomous-start workaround.

### Correct saga and sync facts

The CLI uses direct route request/response rather than fetching all cover paths for start. This is a deliberate limited workflow, **not** a workaround for the former claimed `MowPathSaga` constructor bug: in 0.10.7 `_route_val = route_info if skip_planning else None`. Ordinary planning with `route_info` does plan the route; `skip_planning=True` is for fetching an already-running job's known path.

`MapFetchSaga` fetches the full map, with area names skipped for Luba 1 and multi-frame hash/data acknowledgement handling. Do not repeat the old blanket claim that it always has a five-minute timeout or never exits cleanly. Full-map fetching is unnecessary for this CLI's supported area-name shortcut; Luba 1 needs a different model-specific workflow that is not implemented here.

`send_todev_ble_sync(sync_type=3)` is the upstream **IoT/MQTT app sync** (2 is BLE); it is not solely a BLE radio operation. Upstream sagas use it before major requests. It does not establish MQTT transport readiness, replace freshness checks, or justify forced login.

## Other actions and history

Pause/resume/return/cancel fetch fresh state, validate preconditions, send with `Priority.USER`, then wait for the corresponding fresh state confirmation. Known action builders include `pause_execute_task`, `resume_execute_task`, `return_to_dock`, `cancel_job`, and `start_job`; verify builder names against the pinned source when changing commands.

Resume uses `resume_execute_task` (action=3), not `start_job` (action=1). Schedules use correlated `read_plan` / `todev_planjob_set` replies decoded by `Plan.from_wire`, never cached plans; history uses `query_job_history` / `request_job_history`. If reports temporarily replace/wrap a reducer hook to collect responses, **restore the original hook in `finally`**, including timeout, exception and cancellation paths. Never leave the client reducer patched after a reports command.

Selected `sys_status` values: READY=11, WORKING=13, RETURNING=14, CHARGING=15, PAUSE=19, MANUAL_MOWING=20. These are telemetry, not safety certification.

## Safety contract

The CLI cannot guarantee safety. Cloud pause/cancel/return are ordinary remote requests, never an emergency stop; return may move the mower. Network delay, account contention and confirmation failure can make outcomes uncertain. Use the **physical STOP button** in an emergency. Do not blindly retry an unconfirmed start or resume: inspect fresh state and the physical mower first.

An unresolved IDE import can reflect uv's isolated script environment. Resolve imports against uv's script environment rather than weakening pins or adding unrelated dependencies.
