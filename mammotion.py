#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "pymammotion==0.10.7",
#     "orjson==3.12.0",
#     "betterproto2==0.10.0",
#     "packaging==26.3",  # PyMammotion imports this but omits it from Requires-Dist.
# ]
# ///

import argparse
import asyncio
import logging
import math
import os
import tempfile
import sys
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

import orjson

from pymammotion.client import MammotionClient
from pymammotion.utility.device_type import DeviceType
from pymammotion.data.model.device_config import OperationSettings, build_route_information
from pymammotion.data.model.hash_list import AreaHashNameList, Plan
from pymammotion.messaging.command_queue import Priority
from pymammotion.transport.base import TransportType

# setup logging
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# silence noisy mqtt/asyncio cleanup loggers
for _noisy in ["mqtt", "paho", "paho.mqtt", "aiomqtt", "asyncio"]:
    logging.getLogger(_noisy).setLevel(logging.CRITICAL)

# silence token-refresh noise from pymammotion internals
for _noisy in ["pymammotion.client", "pymammotion.aliyun", "pymammotion.messaging.command_queue"]:
    logging.getLogger(_noisy).setLevel(logging.CRITICAL)

# conversion constants
MM_PER_INCH = 25.4
CM_PER_INCH = 2.54
METERS_TO_MILES = 0.000621371
SQFT_PER_SQM = 10.764

# auth cache
AUTH_CACHE_FILE = Path.home() / '.mammotion.json'


class MammotionWorkMode(Enum):
    """work mode constants with display names."""
    NOT_ACTIVE = (0, "not active")
    ONLINE = (1, "online/idle")
    OFFLINE = (2, "offline")
    DISABLE = (8, "disabled")
    INITIALIZATION = (10, "initializing")
    READY = (11, "ready")
    WORKING = (13, "mowing")
    RETURNING = (14, "returning to dock")
    CHARGING = (15, "charging")
    UPDATING = (16, "updating firmware")
    LOCK = (17, "locked")
    PAUSE = (19, "paused")
    MANUAL_MOWING = (20, "manual mowing")
    UPDATE_SUCCESS = (22, "update complete")
    OTA_UPGRADE_FAIL = (23, "update failed")
    JOB_DRAW = (31, "drawing boundary")
    OBSTACLE_DRAW = (32, "drawing obstacle")
    CHANNEL_DRAW = (34, "drawing channel")
    ERASER_DRAW = (35, "erasing")
    EDIT_BOUNDARY = (36, "editing boundary")
    LOCATION_ERROR = (37, "location error")
    BOUNDARY_JUMP = (38, "boundary error")
    CHARGING_PAUSE = (39, "paused (charging)")

    def __new__(cls, value: int, display: str):
        obj = object.__new__(cls)
        obj._value_ = value
        obj.display = display
        return obj

    @classmethod
    def from_value(cls, value: int) -> "MammotionWorkMode | None":
        for mode in cls:
            if mode.value == value:
                return mode
        return None

    @classmethod
    def display_for(cls, value: int) -> str:
        mode = cls.from_value(value)
        return mode.display if mode else f"unknown ({value})"


class MammotionRtkLevel(Enum):
    """RTK position/fix quality levels."""
    NO_FIX = (0, "no fix")
    SINGLE = (1, "single")
    FLOAT = (2, "float")
    FIX = (4, "fix")

    def __new__(cls, value: int, display: str):
        obj = object.__new__(cls)
        obj._value_ = value
        obj.display = display
        return obj

    @classmethod
    def from_value(cls, value: int) -> "MammotionRtkLevel | None":
        for level in cls:
            if level.value == value:
                return level
        return None

    @classmethod
    def display_for(cls, value: int) -> str:
        level = cls.from_value(value)
        return level.display if level else f"unknown ({value})"



class MammotionCLI:
    """CLI wrapper around the PyMammotion client."""

    def __init__(self):
        self._client: MammotionClient = MammotionClient(ha_version="0.5.7")
        self._client.on_credentials_updated = self._credentials_updated
        self.devices: list[dict[str, Any]] = []
        self.exit_code = 0
        self._authenticated = False
        self._restoring_cache = None

    def _error(self, message: str) -> None:
        self.exit_code = 1
        print(message)

    async def _credentials_updated(self) -> None:
        self._save_cache()

    # === helpers ===

    def is_rtk_device(self, device_name: str) -> bool:
        return device_name.upper().startswith("RTK")

    def check_not_rtk(self, device_name: str) -> bool:
        if self.is_rtk_device(device_name):
            self._error("RTK does not support this command")
            return False
        return True

    def can_pause(self, status: int) -> bool:
        return status == MammotionWorkMode.WORKING.value

    def can_resume(self, status: int) -> bool:
        return status in (MammotionWorkMode.PAUSE.value, MammotionWorkMode.CHARGING_PAUSE.value)

    def can_cancel(self, status: int) -> bool:
        return status in (
            MammotionWorkMode.WORKING.value,
            MammotionWorkMode.PAUSE.value,
            MammotionWorkMode.CHARGING_PAUSE.value,
            MammotionWorkMode.RETURNING.value,
        )

    def can_dock(self, status: int) -> bool:
        return status in (
            MammotionWorkMode.READY.value,
            MammotionWorkMode.WORKING.value,
            MammotionWorkMode.PAUSE.value,
        )

    # === cache ===

    def _save_cache(self) -> None:
        temporary = None
        try:
            cache = self._client.to_cache()
            if cache:
                restoring = getattr(self, '_restoring_cache', None)
                if restoring:
                    # Token rotation can precede transport restoration. Preserve those
                    # cache blocks until the complete restored session is available.
                    cache = {**restoring, **cache}
                # Same-directory atomic replacement avoids partial JSON after interruption.
                with tempfile.NamedTemporaryFile(dir=AUTH_CACHE_FILE.parent, delete=False) as file:
                    temporary = Path(file.name)
                    file.write(orjson.dumps(cache, option=orjson.OPT_INDENT_2))
                    file.flush()
                    os.fsync(file.fileno())
                temporary.chmod(0o600)
                os.replace(temporary, AUTH_CACHE_FILE)
                logger.debug("saved auth cache to %s", AUTH_CACHE_FILE)
        except Exception:
            logger.warning("failed to save auth cache; refreshed credentials may be lost")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _load_cache(self) -> dict | None:
        if not AUTH_CACHE_FILE.exists():
            return None
        try:
            data = orjson.loads(AUTH_CACHE_FILE.read_bytes())
            return data if isinstance(data, dict) and data else None
        except Exception as e:
            logger.warning("failed to load auth cache: %s", e)
            return None

    # === login ===

    async def login(self, email: str, password: str, use_cache: bool = True) -> bool:
        """Restore credentials; upstream alone decides whether a password grant is needed."""
        self._authenticated = False
        try:
            cache = self._load_cache() if use_cache else None
            self._restoring_cache = cache
            if cache:
                await self._client.restore_credentials(email, password, cache)
            else:
                await self._client.login_and_initiate_cloud(email, password)
            self._authenticated = True
            self._restoring_cache = None
            self._save_cache()
            return True
        except Exception as e:
            # Network failures are not evidence that a cached login is invalid.
            self._error(f"login/restore failed (cache preserved): {e}")
            return False

    async def _wait_for_connection(self, device_name: str, timeout: float = 12.0) -> bool:
        """Wait for the target's transport, not an unrelated account connection."""
        handle = self._client.mower(device_name)
        if handle is None:
            return False
        transport_types = (TransportType.CLOUD_ALIYUN, TransportType.CLOUD_MAMMOTION)

        def connected():
            return any(handle.is_transport_connected(kind) for kind in transport_types)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if connected():
                # Aliyun CONNECTED includes bind; Mammotion still needs subscriptions.
                await asyncio.sleep(min(2.0, max(0.0, deadline - loop.time())))
                if connected() and loop.time() <= deadline:
                    return True
            await asyncio.sleep(0.25)
        return False

    # === device listing ===

    async def get_devices(self) -> list[dict[str, Any]]:
        devices = []
        seen: set[str] = set()

        for dev in self._client.aliyun_device_list:
            name = getattr(dev, 'device_name', None)
            if not name or name in seen:
                continue
            seen.add(name)
            devices.append({
                'device_name': name,
                'iot_id': getattr(dev, 'iot_id', ''),
                'product_key': getattr(dev, 'product_key', ''),
                'shared': getattr(dev, 'owned', 1) == 0,
                'nick_name': getattr(dev, 'nick_name', None),
                'product_name': getattr(dev, 'product_name', None),
            })

        for dev in self._client.mammotion_device_list:
            name = getattr(dev, 'device_name', None)
            if not name or name in seen:
                continue
            seen.add(name)
            devices.append({
                'device_name': name,
                'iot_id': getattr(dev, 'iot_id', ''),
                'product_key': getattr(dev, 'product_key', ''),
                'shared': False,
                'nick_name': getattr(dev, 'nick_name', None),
                'product_name': getattr(dev, 'product_name', None),
            })

        self.devices = devices
        return devices

    def find_device(self, device_name: str) -> dict[str, Any] | None:
        for dev in self.devices:
            if dev['device_name'] == device_name:
                return dev
        return None

    # === device state ===

    async def _wait_for_fresh_status(self, device_name: str, predicate, timeout: float = 15.0):
        """Wait for an applied report containing dev, including unchanged telemetry.

        General report timestamps also advance for RTK/work-only packets, while
        state-change subscriptions omit identical reports. This pinned reducer hook
        observes the actual dev report without accepting either source of stale state.
        """
        handle = self._client.mower(device_name)
        if handle is None:
            raise ValueError(f"device not found: {device_name}")
        reached = asyncio.get_running_loop().create_future()
        original_apply = handle._reducer.apply

        def apply(current, message):
            updated = original_apply(current, message)
            report = getattr(getattr(message, 'sys', None), 'toapp_report_data', None)
            if report is not None and report.dev is not None and not reached.done():
                try:
                    if predicate(updated):
                        reached.set_result(updated)
                except Exception as e:
                    reached.set_exception(e)
            return updated

        handle._reducer.apply = apply
        try:
            async with asyncio.timeout(timeout):
                await self._client.refresh_status(device_name)
                return await reached
        finally:
            handle._reducer.apply = original_apply
            if not reached.done():
                reached.cancel()
            elif not reached.cancelled():
                reached.exception()  # consume predicate errors even if refresh itself failed

    async def get_device_state(self, device_name: str) -> dict[str, Any] | None:
        """get current device state via MQTT."""
        try:
            device = await self._wait_for_fresh_status(
                device_name, lambda state: state.report_data.dev.sys_status != 0,
            )

            # progress and time are bit-packed in the work fields
            area_raw = device.report_data.work.area
            progress_raw = device.report_data.work.progress

            # get position from locations list if available
            pos_x, pos_y, heading = 0, 0, 0
            if device.report_data.locations:
                loc = device.report_data.locations[0]
                pos_x = loc.real_pos_x
                pos_y = loc.real_pos_y
                heading = loc.real_toward

            return {
                'status': device.report_data.dev.sys_status,
                'status_name': MammotionWorkMode.display_for(device.report_data.dev.sys_status),
                'docked': device.report_data.dev.charge_state != 0,
                'battery': device.report_data.dev.battery_val,
                'progress': area_raw >> 16,
                'total_time_min': progress_raw & 65535,
                'time_left_min': progress_raw >> 16,
                'pos_x': pos_x,
                'pos_y': pos_y,
                'heading': heading,
                'blade_height': device.report_data.work.knife_height,
                'gps_stars': device.report_data.rtk.gps_stars,
                'co_view_stars': device.report_data.rtk.co_view_stars,
                'rtk_status': device.report_data.rtk.status,
                'rtk_pos_level': device.report_data.rtk.pos_level,
                'rtk_dis_status': device.report_data.rtk.dis_status,
                'lifetime_hours': device.report_data.maintenance.work_time,
                'mileage': device.report_data.maintenance.mileage,
            }

        except Exception as e:
            self._error(f"failed to get fresh device status: {e}")
            return None

    # === area list ===

    async def get_area_list(self, device_name: str) -> list[Any]:
        """fetch area names directly from device — single request/response, no saga."""
        if DeviceType.is_luba1(device_name):
            self._error("Luba 1 area discovery is not supported by this CLI; use the Mammotion app")
            return []
        try:
            # get_area_name_list requires the device's iot_id; look it up from registered devices
            handle = self._client.mower(device_name)
            iot_id = handle.iot_id if handle else ""

            # Decode the fresh reply directly, never use cached area names on failure.
            reply = await self._client.send_command_and_wait(
                device_name,
                "get_area_name_list",
                "toapp_all_hash_name",
                send_timeout=15.0,
                priority=Priority.USER,
                device_id=iot_id,
            )
            return [AreaHashNameList(name=item.name, hash=item.hash)
                    for item in reply.nav.toapp_all_hash_name.hashnames]
        except Exception as e:
            self._error(f"failed to get areas: {e}")
            return []


    # === stop ===

    async def stop(self) -> None:
        # A failed restore may leave only a partial session: do not overwrite a good cache.
        if getattr(self, '_authenticated', False):
            self._save_cache()
        try:
            await self._client.stop()
        except Exception:
            pass

    # === command handlers ===

    async def cmd_devices(self, args) -> None:
        devices = await self.get_devices()

        print("\nMammotion Devices:")
        print("=" * 70)
        print()

        if not devices:
            print("\nNo devices found.")
        else:
            owned = [d for d in devices if not d.get('shared')]
            shared = [d for d in devices if d.get('shared')]

            if owned:
                for dev in owned:
                    print(f"  {dev['device_name']}")

            if shared:
                if owned:
                    print()
                print("  Shared with you:")
                for dev in shared:
                    print(f"    {dev['device_name']}")

            print(f"\n{'=' * 70}")

    async def cmd_status(self, args) -> None:
        print(f"\nStatus for {args.device}:")
        print("=" * 70)
        print()

        if self.is_rtk_device(args.device):
            # RTK base station — look up from device list, no MQTT state query needed
            cloud_dev = None
            for dev in self._client.aliyun_device_list:
                if dev.device_name == args.device:
                    cloud_dev = dev
                    break

            if not cloud_dev:
                self._error(f"device not found: {args.device}")
                return

            print("  Type: RTK Base Station")
            print(f"  Status: {'online' if getattr(cloud_dev, 'status', 0) == 1 else 'offline'}")
            if getattr(cloud_dev, 'product_name', None):
                print(f"  Product: {cloud_dev.product_name}")
            if getattr(cloud_dev, 'product_model', None):
                print(f"  Model: {cloud_dev.product_model}")
        else:
            state = await self.get_device_state(args.device)

            if not state:
                self._error("failed to get device status")
                return

            print(f"  Status: {state['status_name']}")
            print(f"  Docked: {'yes' if state['docked'] else 'no'}")
            print(f"  Battery: {state['battery']}%")

            if state['status'] in (
                MammotionWorkMode.WORKING.value,
                MammotionWorkMode.PAUSE.value,
                MammotionWorkMode.CHARGING_PAUSE.value,
            ):
                print(f"  Progress: {state['progress']}%")
                if state['time_left_min'] > 0:
                    hours = state['time_left_min'] // 60
                    mins = state['time_left_min'] % 60
                    print(f"  Time remaining: {hours}h {mins}m")

            if state['pos_x'] != 0 or state['pos_y'] != 0:
                x_m = state['pos_x'] / 1000
                y_m = state['pos_y'] / 1000
                heading_deg = (state['heading'] / 100) % 360
                print(f"  Position: ({x_m:.1f}m, {y_m:.1f}m) heading {heading_deg:.0f}°")

            if state['blade_height'] > 0:
                blade_height_in = state['blade_height'] / MM_PER_INCH
                print(f"  Blade height: {state['blade_height']}mm ({blade_height_in:.1f}in)")

            if state['gps_stars'] > 0:
                rtk_level = MammotionRtkLevel.display_for(state['rtk_pos_level'])
                print(f"  RTK: {rtk_level} | GPS: {state['gps_stars']} satellites")

            if state['lifetime_hours'] > 0:
                hours = state['lifetime_hours'] // 3600
                print(f"  Lifetime work time: {hours}h")
            if state['mileage'] > 0:
                miles = state['mileage'] * METERS_TO_MILES
                print(f"  Total mileage: {miles:.1f} miles")

        print()
        print("=" * 70)

    async def cmd_start(self, args) -> None:
        if not self.check_not_rtk(args.device):
            return

        if not DeviceType.is_luba_pro(args.device) or DeviceType.is_yuka(args.device):
            self._error("start supports Luba 2/Pro H-range models only; use the Mammotion app for other models")
            return

        # Reject NaN/Infinity as well as out-of-range values before creating payloads.
        if not all(math.isfinite(value) for value in (args.speed, args.cutting_height, args.path_spacing)):
            self._error("error: speed, cutting height and path spacing must be finite")
            return

        # validate inputs
        if args.speed < 0.0 or args.speed > 1.0:
            self._error(f"error: speed must be between 0.0 and 1.0 (got {args.speed})")
            return

        if args.cutting_height < 2.2 or args.cutting_height > 3.9:
            self._error(f"error: cutting height must be between 2.2in and 3.9in (got {args.cutting_height}in)")
            return

        if args.path_spacing < 7.9 or args.path_spacing > 13.8:
            self._error(f"error: path spacing must be between 7.9in and 13.8in (got {args.path_spacing}in)")
            return

        if args.perimeter_laps < 0 or args.perimeter_laps > 4:
            self._error(f"error: perimeter laps must be between 0 and 4 (got {args.perimeter_laps})")
            return

        if args.mowing_angle < 0 or args.mowing_angle > 359:
            self._error(f"error: mowing angle must be between 0 and 359 degrees (got {args.mowing_angle})")
            return

        # convert pattern string to channel_mode int
        pattern_map = {'perimeter': 3, 'zigzag': 0, 'chessboard': 1, 'adaptive': 2}
        channel_mode = pattern_map[args.pattern]

        # convert mow_order to border_mode (0=perimeter first, 1=grid first)
        border_mode = 0 if args.mow_order == 'perimeter-first' else 1

        # convert inches to millimeters/centimeters for api
        # blade height must be a multiple of 5mm in the range [55, 100]
        blade_height_mm = round(args.cutting_height * MM_PER_INCH / 5) * 5
        blade_height_mm = max(55, min(100, blade_height_mm))
        path_spacing_cm = int(args.path_spacing * CM_PER_INCH)

        # get areas from device
        print("fetching areas...")
        areas = await self.get_area_list(args.device)
        if not areas:
            self._error("failed to get areas - cannot start task")
            return

        # resolve area names/hashes from arguments
        area_hashes = []
        for area_input in args.areas:
            matched = False
            for area in areas:
                if area.name == area_input or str(area.hash) == area_input:
                    area_hashes.append(area.hash)
                    matched = True
                    print(f"  - {area.name} (hash: {area.hash})")
                    break
            if not matched:
                self._error(f"error: area '{area_input}' not found")
                print(f"available areas: {', '.join([a.name for a in areas])}")
                return

        if not area_hashes:
            self._error("error: no valid areas specified")
            return

        print(f"\ngenerating route for {len(area_hashes)} area(s)...")
        print(f"  pattern: {args.pattern}, spacing: {args.path_spacing}in ({path_spacing_cm}cm), perimeter laps: {args.perimeter_laps}")
        print(f"  mow order: {args.mow_order}, speed: {args.speed}, cutting height: {args.cutting_height}in ({blade_height_mm}mm), angle: {args.mowing_angle}°")

        # Delegate model-specific flags and firmware gates to the pinned upstream builder.
        state = await self.get_device_state(args.device)
        if not state:
            return
        if state['status'] not in (MammotionWorkMode.READY.value, MammotionWorkMode.ONLINE.value,
                                   MammotionWorkMode.CHARGING.value):
            self._error(f"cannot start: device is {state['status_name']}")
            return
        settings = OperationSettings(
            areas=area_hashes,
            speed=args.speed,
            blade_height=blade_height_mm,
            ultra_wave=2,
            channel_mode=channel_mode,
            channel_width=path_spacing_cm,
            mowing_laps=args.perimeter_laps,
            obstacle_laps=1,
            job_mode=4,
            toward=args.mowing_angle,
            toward_included_angle=90,
            toward_mode=1,
            border_mode=border_mode,
            is_dump=False,
        )
        route_info = build_route_information(
            args.device, self._client.get_device_by_name(args.device), settings
        )

        try:
            # Explicitly await route confirmation before sending start_job.
            print("planning route...")
            await self._client.send_command_and_wait(
                args.device,
                "generate_route_information",
                "bidire_reqconver_path",
                send_timeout=30.0,
                priority=Priority.USER,
                generate_route_information=route_info,
            )

            await self._send_action(args.device, "start_job", {MammotionWorkMode.WORKING.value})
            print(f"started mowing task on {args.device}")

        except Exception as e:
            self._error(f"start command failed or was not confirmed: {e}")

    async def _send_action(self, device_name: str, command: str, expected_statuses: set[int]) -> None:
        """Await direct delivery and fresh status confirmation; never repeat an uncertain action."""
        if self._client.mower(device_name) is None:
            raise ValueError(f"device not found: {device_name}")
        await self._client.send_command_with_args(device_name, command, priority=Priority.USER)
        try:
            await self._wait_for_fresh_status(
                device_name,
                lambda state: state.report_data.dev.sys_status in expected_statuses
                and (command != "return_to_dock"
                     or state.report_data.dev.sys_status != MammotionWorkMode.READY.value
                     or state.report_data.dev.charge_state != 0),
                timeout=30.0,
            )
        except Exception as e:
            raise RuntimeError(
                f"{command} was sent but not confirmed; check device status before retrying: {e}"
            ) from e

    async def _run_command_with_state_check(
        self,
        args,
        can_run_fn,
        cmd: str,
        success_msg: str,
        blocked_msg: str,
        hint: str = "",
    ) -> None:
        """check device state then send command if state allows it."""
        if not self.check_not_rtk(args.device):
            return

        state = await self.get_device_state(args.device)
        if not state:
            self._error("failed to get device status")
            return

        if not can_run_fn(state['status']):
            self._error(f"{blocked_msg}: device is {state['status_name']}")
            if hint:
                print(hint)
            return

        expected = {
            "pause_execute_task": {MammotionWorkMode.PAUSE.value, MammotionWorkMode.CHARGING_PAUSE.value},
            "resume_execute_task": {MammotionWorkMode.WORKING.value},
            "return_to_dock": {MammotionWorkMode.RETURNING.value, MammotionWorkMode.CHARGING.value,
                               MammotionWorkMode.READY.value},
            "cancel_job": {MammotionWorkMode.ONLINE.value, MammotionWorkMode.READY.value,
                           MammotionWorkMode.CHARGING.value},
        }
        try:
            await self._send_action(args.device, cmd, expected[cmd])
            print(success_msg)
        except Exception as e:
            self._error(f"{cmd} failed or was not confirmed: {e}")

    async def cmd_pause(self, args) -> None:
        await self._run_command_with_state_check(
            args,
            can_run_fn=self.can_pause,
            cmd="pause_execute_task",
            success_msg=f"paused {args.device}",
            blocked_msg="cannot pause",
            hint="pause only works when mowing is in progress",
        )

    async def cmd_resume(self, args) -> None:
        await self._run_command_with_state_check(
            args,
            can_run_fn=self.can_resume,
            cmd="resume_execute_task",
            success_msg=f"resumed mowing on {args.device}",
            blocked_msg="cannot resume",
            hint="resume only works when mowing is paused",
        )

    async def cmd_return(self, args) -> None:
        await self._run_command_with_state_check(
            args,
            can_run_fn=self.can_dock,
            cmd="return_to_dock",
            success_msg=f"{args.device} returning to dock",
            blocked_msg="cannot return to dock",
        )

    async def cmd_cancel(self, args) -> None:
        await self._run_command_with_state_check(
            args,
            can_run_fn=self.can_cancel,
            cmd="cancel_job",
            success_msg=f"cancelled task on {args.device}",
            blocked_msg="cannot cancel",
            hint="cancel only works when a task is active",
        )

    async def cmd_areas(self, args) -> None:
        if not self.check_not_rtk(args.device):
            return

        areas = await self.get_area_list(args.device)

        print(f"\nAreas for {args.device}:")
        print("=" * 70)
        print()

        if areas:
            for area in areas:
                print(f"  {area.name} (hash: {area.hash})")
            print(f"\n{'=' * 70}")
        else:
            print("  No areas found.")
            print("  Try running again or check if map exists in the app.")

    async def cmd_schedule(self, args) -> None:
        if not self.check_not_rtk(args.device):
            return

        if not self._client.mower(args.device):
            self._error(f"device not found: {args.device}")
            return

        try:
            areas = await self.get_area_list(args.device)
            area_map = {area.hash: area.name for area in areas} if areas else {}

            # Decode only fresh replies; cached plans must not masquerade as this fetch.
            plans = {}
            index = 0
            expected_total = None
            while True:
                reply = await self._client.send_command_and_wait(
                    args.device, "read_plan", "todev_planjob_set",
                    sub_cmd=2, plan_index=index, send_timeout=10.0, priority=Priority.USER,
                )
                plan = Plan.from_wire(reply.nav.todev_planjob_set, args.device)
                total = plan.total_plan_num
                if expected_total is None:
                    expected_total = total
                if total < 0 or total != expected_total:
                    raise RuntimeError("schedule count changed during fetch; retry inspection")
                if total == 0:
                    break
                if not plan.plan_id or plan.plan_id in plans:
                    raise RuntimeError("device returned a missing or repeated schedule ID")
                plans[plan.plan_id] = plan
                if len(plans) == total:
                    break
                if len(plans) > total:
                    raise RuntimeError("device returned an inconsistent schedule count")
                index += 1

            print(f"\nSchedules for {args.device}:")
            print("=" * 70)

            if not plans:
                print("\nNo scheduled tasks found.")
                print("Schedules can be created in the Mammotion app.")
            else:
                # day of week mapping (some devices use 0=Sun, others use 7=Sun)
                day_names = {0: "Sun", 1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}
                pattern_names = {0: "zigzag", 1: "chessboard", 2: "adaptive", 3: "perimeter"}

                for idx, (plan_id, plan) in enumerate(plans.items(), 1):
                    print(f"\n  [{idx}/{len(plans)}] Schedule: {plan.task_name or plan.job_name or plan_id}")

                    if plan.start_time:
                        print(f"  Start time:  {plan.start_time}")
                    if plan.end_time:
                        print(f"  End time:    {plan.end_time}")
                    if plan.start_date:
                        print(f"  Start date:  {plan.start_date}")
                    if plan.end_date:
                        print(f"  End date:    {plan.end_date}")

                    if plan.weeks:
                        days = [day_names.get(d, str(d)) for d in plan.weeks]
                        print(f"  Days:        {', '.join(days)}")
                    elif plan.week:
                        print(f"  Day:         {day_names.get(plan.week, str(plan.week))}")

                    if plan.zone_hashs:
                        zone_names = [area_map.get(zh, f"hash:{zh}") for zh in plan.zone_hashs]
                        print(f"  Areas:       {', '.join(zone_names)}")

                    if plan.knife_height > 0:
                        height_in = plan.knife_height / MM_PER_INCH
                        print(f"  Blade:       {plan.knife_height}mm ({height_in:.1f}\")")

                    if plan.route_model is not None:
                        pattern = pattern_names.get(plan.route_model, f"mode {plan.route_model}")
                        print(f"  Pattern:     {pattern}")

                    if plan.route_spacing > 0:
                        spacing_in = plan.route_spacing / CM_PER_INCH
                        print(f"  Spacing:     {plan.route_spacing}cm ({spacing_in:.1f}\")")

                    if plan.speed > 0:
                        print(f"  Speed:       {plan.speed}")

                    if plan.edge_mode > 0:
                        print(f"  Border laps: {plan.edge_mode}")

                    if args.verbose:
                        print(f"  [DEBUG] plan_id={plan.plan_id}, task_id={plan.task_id}")
                        print(f"  [DEBUG] work_time={plan.work_time}, required_time={plan.required_time}, area={plan.area}")

                print(f"\n{'=' * 70}")

        except Exception as e:
            self._error(f"failed to get schedule: {e}")

    async def cmd_reports(self, args) -> None:
        if not self.check_not_rtk(args.device):
            return

        if not self._client.mower(args.device):
            self._error(f"device not found: {args.device}")
            return

        if args.count <= 0:
            self._error("error: report count must be positive")
            return

        handle = None
        original_apply = None
        try:
            # wrap the reducer's apply() to intercept work report messages before
            # they're deep-copied and overwrite the previous record — this is the
            # only reliable hook since the reducer deep-copies work_session_result
            # on every toapp_work_report_ack, making subclassing useless
            handle = self._client.mower(args.device)
            if not handle:
                self._error(f"device not found: {args.device}")
                return
            work_reports: list[dict] = []
            original_apply = handle._reducer.apply

            def _patched_apply(current, message):
                import betterproto2
                nav_msg_name = ""
                res = betterproto2.which_one_of(message, "LubaSubMsg")
                if res[0] == "nav":
                    nav_msg_name = betterproto2.which_one_of(message.nav, "SubNavMsg")[0]
                result = original_apply(current, message)
                if nav_msg_name in ("toapp_work_report_ack", "toapp_work_report_upload"):
                    r = result.work_session_result
                    if r.start_work_time > 0:
                        work_reports.append({
                            'interrupt_flag': r.interrupt_flag,
                            'start_work_time': r.start_work_time,
                            'end_work_time': r.end_work_time,
                            'work_time_used': r.work_time_used,
                            'work_area': r.work_area,
                            'work_progress': r.work_progress,
                            'height_of_knife': r.height_of_knife,
                            'work_type': r.work_type,
                            'work_result': r.work_result,
                        })
                return result

            handle._reducer.apply = _patched_apply

            await self._client.send_command_with_args(args.device, "query_job_history", priority=Priority.USER)

            await asyncio.sleep(2)

            await self._client.send_command_with_args(args.device, "request_job_history", priority=Priority.USER, num=args.count)

            # wait for records to arrive; poll until count reached or 10s with no new record
            prev_count = 0
            stale_polls = 0
            for _ in range(120):  # up to 60s total
                await asyncio.sleep(0.5)
                cur_count = len(work_reports)
                if cur_count >= args.count:
                    break
                if cur_count > prev_count:
                    stale_polls = 0
                    prev_count = cur_count
                else:
                    stale_polls += 1
                    if stale_polls >= 20 and cur_count > 0:
                        # 10s with no new record — device is done sending
                        break

            if not args.content_only:
                print(f"\nMowing History for {args.device}:")
                print("=" * 70)

            if not work_reports:
                print("\nNo mowing reports available")
            else:
                # sort by start time (newest first)
                work_reports.sort(key=lambda r: r['start_work_time'], reverse=True)

                if args.today_only:
                    today = datetime.now().strftime("%Y-%m-%d")
                    work_reports = [
                        r for r in work_reports
                        if r['start_work_time'] > 0
                        and datetime.fromtimestamp(r['start_work_time']).strftime("%Y-%m-%d") == today
                    ]

                # work type and result name maps
                work_type_names = {
                    0: "Unknown",
                    1: "Mowing",
                    2: "Border First",
                    3: "Border Only",
                    4: "Task Mode",
                    8: "Manual Mode",
                }
                result_names = {
                    0: "In Progress",
                    1: "Failed",
                    2: "Canceled",
                    3: "Stopped",
                    4: "Paused",
                    5: "Completed",
                }

                for idx, report in enumerate(work_reports, 1):
                    print(f"\n  [{idx}/{len(work_reports)}] Mowing Report:")

                    # timestamps
                    if report['start_work_time'] > 0:
                        start = datetime.fromtimestamp(report['start_work_time'])
                        print(f"  Started:     {start.strftime('%Y-%m-%d %H:%M:%S')}")

                    if report['end_work_time'] > 0:
                        end = datetime.fromtimestamp(report['end_work_time'])
                        print(f"  Ended:       {end.strftime('%Y-%m-%d %H:%M:%S')}")

                    # duration
                    if report['work_time_used'] > 0:
                        hours = report['work_time_used'] // 3600
                        minutes = (report['work_time_used'] % 3600) // 60
                        print(f"  Duration:    {hours}h {minutes}m")

                    # area
                    if report['work_area'] > 0:
                        sqft = report['work_area'] * SQFT_PER_SQM
                        print(f"  Area:        {report['work_area']:.1f} m² ({sqft:.0f} ft²)")

                    # blade height
                    if report['height_of_knife'] > 0:
                        inches = report['height_of_knife'] / MM_PER_INCH
                        print(f"  Blade:       {report['height_of_knife']}mm ({inches:.1f}\")")

                    # progress
                    if report['work_progress'] > 0:
                        print(f"  Progress:    {report['work_progress']}%")

                    # work type
                    wt = report['work_type']
                    if wt > 0:
                        print(f"  Work Type:   {work_type_names.get(wt, f'Type {wt}')}")

                    # result status
                    if report['interrupt_flag']:
                        print("  Result:      Interrupted")
                    else:
                        wr = report['work_result']
                        print(f"  Result:      {result_names.get(wr, f'Unknown ({wr})')}")

                    # verbose debug output
                    if args.verbose:
                        print(f"  [DEBUG] work_type={report['work_type']}, work_result={report['work_result']}")

                if not args.content_only:
                    print(f"\n{'=' * 70}")

        except Exception as e:
            self._error(f"failed to get mow reports: {e}")
        finally:
            if handle is not None and original_apply is not None:
                handle._reducer.apply = original_apply

    async def run(self, args) -> int:
        try:
            # get credentials from env or args
            email = args.email or os.environ.get('MAMMOTION_EMAIL')
            password = args.password or os.environ.get('MAMMOTION_PASSWORD')

            if not email or not password:
                self._error("error: email and password required (via args or MAMMOTION_EMAIL/MAMMOTION_PASSWORD env vars)")
                return 1

            # login (use cache unless --no-cache specified)
            use_cache = not getattr(args, 'no_cache', False)
            if not await self.login(email, password, use_cache=use_cache):
                return 1

            # Listing and RTK metadata don't require mower MQTT readiness.
            device_name = getattr(args, 'device', None)
            if device_name and self.is_rtk_device(device_name) and args.command != 'status':
                self._error("RTK does not support this command")
                return 1
            if device_name and not (args.command == 'status' and self.is_rtk_device(device_name)):
                if self._client.mower(device_name) is None:
                    self._error(f"device not found: {device_name}")
                    return 1
                if not await self._wait_for_connection(device_name):
                    self._error("error: target MQTT connection unavailable; cache preserved. "
                                "Check networking and other Mammotion clients sharing the account.")
                    return 1

            # run command
            if hasattr(args, 'func'):
                await args.func(args)

            return self.exit_code
        except Exception as e:
            self._error(f"command failed: {e}")
            return 1
        finally:
            await self.stop()


def main():
    parser = argparse.ArgumentParser(description='mammotion mower control cli')
    parser.add_argument('-e', '--email', help='account email (or set MAMMOTION_EMAIL)')
    parser.add_argument('-p', '--password', help='account password (or set MAMMOTION_PASSWORD)')
    parser.add_argument('--no-cache', action='store_true', help='skip cached auth, force fresh login')

    subparsers = parser.add_subparsers(dest='command', help='commands')

    # devices command
    devices_parser = subparsers.add_parser('devices', help='list all devices')
    devices_parser.set_defaults(func=lambda ctl: lambda args: ctl.cmd_devices(args))

    # status command
    status_parser = subparsers.add_parser('status', help='show device status')
    status_parser.add_argument('--device', required=True, help='device name')
    status_parser.set_defaults(func=lambda ctl: lambda args: ctl.cmd_status(args))

    # start command
    start_parser = subparsers.add_parser('start', help='start mowing task with specified areas')
    start_parser.add_argument('--device', required=True, help='device name')
    start_parser.add_argument('--areas', required=True, nargs='+', help='area names or hashes to mow (quote names containing spaces)')
    start_parser.add_argument('--pattern', type=str, default='zigzag', choices=['perimeter', 'zigzag', 'chessboard', 'adaptive'], help='mowing path pattern: perimeter=perimeter only, zigzag=single pass (default), chessboard=cross/chess pattern, adaptive=adaptive zigzag')
    start_parser.add_argument('--cutting-height', type=float, default=2.5, help='cutting height in inches (2.2-3.9in, snapped to nearest 5mm), default: 2.5in')
    start_parser.add_argument('--path-spacing', type=float, default=10.0, help='spacing between mowing paths in inches (7.9-13.8in), default: 10.0in')
    start_parser.add_argument('--perimeter-laps', type=int, default=2, help='number of border/perimeter laps (0-4), default: 2')
    start_parser.add_argument('--mow-order', type=str, default='grid-first', choices=['perimeter-first', 'grid-first'], help='mowing order: perimeter-first=border then zigzag, grid-first=zigzag then border (default)')
    start_parser.add_argument('--mowing-angle', type=int, default=0, help='mowing angle in degrees (0-359), controls direction of mowing lines, default: 0 (east/west)')
    start_parser.add_argument('--speed', type=float, default=0.25, help='mowing speed: 0.0 (slow) to 1.0 (fast), default: 0.25')
    start_parser.set_defaults(func=lambda ctl: lambda args: ctl.cmd_start(args))

    # pause command
    pause_parser = subparsers.add_parser('pause', help='pause current mowing job')
    pause_parser.add_argument('--device', required=True, help='device name')
    pause_parser.set_defaults(func=lambda ctl: lambda args: ctl.cmd_pause(args))

    # resume command
    resume_parser = subparsers.add_parser('resume', help='resume paused mowing job')
    resume_parser.add_argument('--device', required=True, help='device name')
    resume_parser.set_defaults(func=lambda ctl: lambda args: ctl.cmd_resume(args))

    # return command
    return_parser = subparsers.add_parser('return', help='return to dock')
    return_parser.add_argument('--device', required=True, help='device name')
    return_parser.set_defaults(func=lambda ctl: lambda args: ctl.cmd_return(args))

    # cancel command
    cancel_parser = subparsers.add_parser('cancel', help='cancel current job')
    cancel_parser.add_argument('--device', required=True, help='device name')
    cancel_parser.set_defaults(func=lambda ctl: lambda args: ctl.cmd_cancel(args))

    # areas command
    areas_parser = subparsers.add_parser('areas', help='list all areas/zones')
    areas_parser.add_argument('--device', required=True, help='device name')
    areas_parser.set_defaults(func=lambda ctl: lambda args: ctl.cmd_areas(args))

    # schedule command
    schedules_parser = subparsers.add_parser('schedule', help='list scheduled mowing tasks')
    schedules_parser.add_argument('--device', required=True, help='device name')
    schedules_parser.add_argument('--verbose', '-v', action='store_true', help='show additional debugging information')
    schedules_parser.set_defaults(func=lambda ctl: lambda args: ctl.cmd_schedule(args))

    # reports command
    reports_parser = subparsers.add_parser('reports', help='get mowing job history reports')
    reports_parser.add_argument('--device', required=True, help='device name')
    reports_parser.add_argument('--count', type=int, default=10, help='number of reports to retrieve (default: 10)')
    reports_parser.add_argument('--verbose', '-v', action='store_true', help='show additional debugging information')
    reports_parser.add_argument('--content-only', action='store_true', help='omit header and divider bars')
    reports_parser.add_argument('--today-only', action='store_true', help='filter output to reports from today only')
    reports_parser.set_defaults(func=lambda ctl: lambda args: ctl.cmd_reports(args))

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    client = MammotionCLI()

    if hasattr(args, 'func'):
        args.func = args.func(client)

    return asyncio.run(client.run(args))


if __name__ == '__main__':
    sys.exit(main())
