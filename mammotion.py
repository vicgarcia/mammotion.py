#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "pymammotion==0.10.12",
#     "orjson==3.13.0",
#     "betterproto2==0.10.0",
#     "packaging==26.3",  # PyMammotion imports this but omits it from Requires-Dist.
# ]
# ///

"""Mammotion mower control: a CLI script and an importable library in one file.

As a CLI (agent skill / shell):

    uv run --script mammotion.py status --device Luba-XXXX

As a library (copy this file next to your code; install the dependencies above):

    from mammotion import Mammotion, MammotionError, run_async

    async def check():
        async with Mammotion(email, password) as mower:
            return await mower.get_device_state("Luba-XXXX")

    state = run_async(check())

Library methods return data and raise MammotionError; they never print. Actions
confirm against fresh mower state and are never retried. None of them is an
emergency stop.
"""

import argparse
import asyncio
import gc
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
from pymammotion.http.model.work_report import WorkReportResult, WorkReportType
from pymammotion.messaging.command_queue import Priority
from pymammotion.transport.base import ReLoginRequiredError, SessionExpiredError, TransportType

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

# auth cache (MAMMOTION_CACHE_DIR relocates it, e.g. onto a container bind mount)
AUTH_CACHE_FILE = Path(os.environ.get('MAMMOTION_CACHE_DIR') or Path.home()) / '.mammotion.json'

# start option values -> route settings
PATTERNS = {'zigzag': 0, 'chessboard': 1, 'adaptive': 2, 'perimeter': 3}
MOW_ORDERS = {'perimeter-first': 0, 'grid-first': 1}

# report codes, using upstream's copies of the app's report-screen enums.
# work_type is how the job was launched -- not a mow order or pattern.
WORK_TYPE_NAMES = {
    WorkReportType.SINGLE: "single job",
    WorkReportType.PLAN: "task / schedule",
    WorkReportType.DROPMOW: "drop mow",
    WorkReportType.RESUME: "resumed job",
}
WORK_RESULT_NAMES = {
    WorkReportResult.UNKNOWN: "unknown",
    WorkReportResult.WORKING: "in progress",
    WorkReportResult.PAUSED: "paused",
    WorkReportResult.USER_ENDED: "ended by user",
    WorkReportResult.INTERRUPTED: "interrupted",
    WorkReportResult.COMPLETED: "completed",
}


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


class MammotionError(RuntimeError):
    """A command failed, was rejected, or its outcome could not be confirmed."""


def blade_height_mm(inches: float) -> int:
    """Blade height in mm: a multiple of 5 in [55, 100] (Luba 2 / Pro H range)."""
    return max(55, min(100, round(inches * MM_PER_INCH / 5) * 5))


def report_times(report: dict[str, Any]) -> tuple[int, int, int]:
    """(elapsed, mowing, other) seconds for a report.

    Elapsed is start to end on the clock; mowing is the mower's own work time;
    other is the remainder — mid-job recharging plus any pauses. Elapsed is 0
    when the report has no end time.
    """
    start, end = report.get('start_work_time', 0), report.get('end_work_time', 0)
    mowing = max(0, report.get('work_time_used', 0))
    elapsed = end - start if start > 0 and end > start else 0
    return elapsed, mowing, max(0, elapsed - mowing) if elapsed else 0


def format_duration(seconds: int) -> str:
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def run_async(coroutine):
    """asyncio.run with the event loop aiomqtt needs on every platform.

    aiomqtt uses add_reader/add_writer/remove_writer, unsupported by Windows'
    default ProactorEventLoop. loop_factory avoids deprecated policy overrides.
    """
    loop_factory = asyncio.SelectorEventLoop if sys.platform == 'win32' else None
    return asyncio.run(coroutine, loop_factory=loop_factory)


class Mammotion:
    """Library API around the PyMammotion client: one cloud session.

    Use as an async context manager (logs in on enter, saves the session cache
    and disconnects on exit), or call login() / stop() yourself.
    """

    def __init__(self, email: str | None = None, password: str | None = None, use_cache: bool = True):
        self._client: MammotionClient = MammotionClient(ha_version="0.5.7")
        self._client.on_credentials_updated = self._credentials_updated
        self._email = email or os.environ.get('MAMMOTION_EMAIL')
        self._password = password or os.environ.get('MAMMOTION_PASSWORD')
        self._use_cache = use_cache
        self.devices: list[dict[str, Any]] = []
        self._authenticated = False
        self._restoring_cache = None
        self._ready_devices: set[str] = set()

    async def __aenter__(self) -> "Mammotion":
        if not self._email or not self._password:
            raise MammotionError("email and password required (or MAMMOTION_EMAIL/MAMMOTION_PASSWORD)")
        try:
            await self.login(self._email, self._password, use_cache=self._use_cache)
        except BaseException:
            await self.stop()
            raise
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.stop()

    async def _credentials_updated(self) -> None:
        self._save_cache()

    # === helpers ===

    def is_rtk_device(self, device_name: str) -> bool:
        return device_name.upper().startswith("RTK")

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

    # === login / readiness ===

    async def login(self, email: str, password: str, use_cache: bool = True) -> None:
        """Restore credentials, with one password grant only when a cached token is rejected."""
        self._authenticated = False
        try:
            cache = self._load_cache() if use_cache else None
            self._restoring_cache = cache
            if cache:
                try:
                    await self._client.restore_credentials(email, password, cache)
                except (SessionExpiredError, ReLoginRequiredError) as e:
                    # An expired cached token is an auth rejection, not a network fault.
                    # Upstream CloudIOTGateway.from_cache raises on an expired Aliyun
                    # refresh token (2460) instead of rebuilding from the valid login.
                    logger.warning("cached session rejected (%s); logging in again", e)
                    self._restoring_cache = None
                    await self._client.login_and_initiate_cloud(email, password)
            else:
                await self._client.login_and_initiate_cloud(email, password)
            self._authenticated = True
            self._restoring_cache = None
            self._save_cache()
        except Exception as e:
            # Network failures are not evidence that a cached login is invalid.
            raise MammotionError(f"login/restore failed (cache preserved): {e}") from e

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

    async def _require_mower(self, device_name: str) -> None:
        """Reject RTK/unknown devices and wait once for the selected mower's transport."""
        if self.is_rtk_device(device_name):
            raise MammotionError("RTK does not support this command")
        if self._client.mower(device_name) is None:
            raise MammotionError(f"device not found: {device_name}")
        if device_name in self._ready_devices:
            return
        # A readiness timeout is a connection failure, not a reason to log in again.
        if not await self._wait_for_connection(device_name):
            raise MammotionError("target MQTT connection unavailable; cache preserved. "
                                 "Check networking and other Mammotion clients sharing the account.")
        self._ready_devices.add(device_name)

    # === device listing ===

    async def get_devices(self) -> list[dict[str, Any]]:
        """Account devices (mowers and RTK stations) from the cloud listing; no mower traffic."""
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

    async def get_device_state(self, device_name: str) -> dict[str, Any]:
        """Fresh mower state (never cached telemetry); raises MammotionError on failure."""
        await self._require_mower(device_name)
        try:
            device = await self._wait_for_fresh_status(
                device_name, lambda state: state.report_data.dev.sys_status != 0,
            )
        except Exception as e:
            raise MammotionError(f"failed to get fresh device status: {e}") from e

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

    # === area list ===

    async def get_area_list(self, device_name: str) -> list[AreaHashNameList]:
        """Area names/hashes fetched fresh from the mower — single request/response, no saga."""
        if DeviceType.is_luba1(device_name):
            raise MammotionError("Luba 1 area discovery is not supported by this CLI; use the Mammotion app")
        await self._require_mower(device_name)
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
            raise MammotionError(f"failed to get areas: {e}") from e

    # === start ===

    async def start_mowing(
        self,
        device_name: str,
        areas: list[str],
        *,
        pattern: str = 'zigzag',
        cutting_height: float = 2.5,
        path_spacing: float = 10.0,
        perimeter_laps: int = 2,
        mow_order: str = 'grid-first',
        mowing_angle: int = 0,
        speed: float = 0.25,
    ) -> list[AreaHashNameList]:
        """Plan a route over the named areas and start it, confirmed by a fresh WORKING report.

        Supports Luba 2 / Pro H-range models only; the caller must verify the H variant.
        Returns the resolved areas. Never retried: on MammotionError inspect fresh state.
        """
        if self.is_rtk_device(device_name):
            raise MammotionError("RTK does not support this command")
        if not DeviceType.is_luba_pro(device_name) or DeviceType.is_yuka(device_name):
            raise MammotionError("start supports Luba 2/Pro H-range models only; use the Mammotion app for other models")

        # Reject NaN/Infinity as well as out-of-range values before creating payloads.
        if not all(math.isfinite(value) for value in (speed, cutting_height, path_spacing)):
            raise MammotionError("speed, cutting height and path spacing must be finite")
        if speed < 0.0 or speed > 1.0:
            raise MammotionError(f"speed must be between 0.0 and 1.0 (got {speed})")
        if cutting_height < 2.2 or cutting_height > 3.9:
            raise MammotionError(f"cutting height must be between 2.2in and 3.9in (got {cutting_height}in)")
        if path_spacing < 7.9 or path_spacing > 13.8:
            raise MammotionError(f"path spacing must be between 7.9in and 13.8in (got {path_spacing}in)")
        if perimeter_laps < 0 or perimeter_laps > 4:
            raise MammotionError(f"perimeter laps must be between 0 and 4 (got {perimeter_laps})")
        if mowing_angle < 0 or mowing_angle > 359:
            raise MammotionError(f"mowing angle must be between 0 and 359 degrees (got {mowing_angle})")
        if pattern not in PATTERNS:
            raise MammotionError(f"pattern must be one of {', '.join(PATTERNS)} (got {pattern})")
        if mow_order not in MOW_ORDERS:
            raise MammotionError(f"mow order must be one of {', '.join(MOW_ORDERS)} (got {mow_order})")
        if not areas:
            raise MammotionError("no areas specified")

        await self._require_mower(device_name)

        # resolve area names/hashes against the mower's fresh list
        available = await self.get_area_list(device_name)
        if not available:
            raise MammotionError("failed to get areas - cannot start task")
        resolved = []
        for area_input in areas:
            match = next((a for a in available if a.name == area_input or str(a.hash) == area_input), None)
            if match is None:
                raise MammotionError(f"area '{area_input}' not found; available areas: "
                                     f"{', '.join(a.name for a in available)}")
            resolved.append(match)

        state = await self.get_device_state(device_name)
        if state['status'] not in (MammotionWorkMode.READY.value, MammotionWorkMode.ONLINE.value,
                                   MammotionWorkMode.CHARGING.value):
            raise MammotionError(f"cannot start: device is {state['status_name']}")

        # Delegate model-specific flags and firmware gates to the pinned upstream builder.
        settings = OperationSettings(
            areas=[a.hash for a in resolved],
            speed=speed,
            blade_height=blade_height_mm(cutting_height),
            ultra_wave=2,
            channel_mode=PATTERNS[pattern],
            channel_width=int(path_spacing * CM_PER_INCH),
            mowing_laps=perimeter_laps,
            obstacle_laps=1,
            job_mode=4,
            toward=mowing_angle,
            toward_included_angle=90,
            toward_mode=1,
            border_mode=MOW_ORDERS[mow_order],
            is_dump=False,
        )
        route_info = build_route_information(
            device_name, self._client.get_device_by_name(device_name), settings
        )

        try:
            # Explicitly await route confirmation before sending start_job.
            await self._client.send_command_and_wait(
                device_name,
                "generate_route_information",
                "bidire_reqconver_path",
                send_timeout=30.0,
                priority=Priority.USER,
                generate_route_information=route_info,
            )
            await self._send_action(device_name, "start_job", {MammotionWorkMode.WORKING.value})
        except Exception as e:
            raise MammotionError(f"start command failed or was not confirmed: {e}") from e
        return resolved

    # === other actions ===

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
            raise MammotionError(
                f"{command} was sent but not confirmed; check device status before retrying: {e}"
            ) from e

    async def _action(self, device_name: str, command: str, can_run, expected: set[int],
                      blocked: str, hint: str = "") -> None:
        """Check fresh state allows the action, send it, and wait for the confirming state."""
        state = await self.get_device_state(device_name)
        if not can_run(state['status']):
            raise MammotionError(f"{blocked}: device is {state['status_name']}" + (f" ({hint})" if hint else ""))
        try:
            await self._send_action(device_name, command, expected)
        except MammotionError:
            raise
        except Exception as e:
            raise MammotionError(f"{command} failed or was not confirmed: {e}") from e

    async def pause(self, device_name: str) -> None:
        await self._action(device_name, "pause_execute_task", self.can_pause,
                           {MammotionWorkMode.PAUSE.value, MammotionWorkMode.CHARGING_PAUSE.value},
                           "cannot pause", "pause only works when mowing is in progress")

    async def resume(self, device_name: str) -> None:
        await self._action(device_name, "resume_execute_task", self.can_resume,
                           {MammotionWorkMode.WORKING.value},
                           "cannot resume", "resume only works when mowing is paused")

    async def return_to_dock(self, device_name: str) -> None:
        """Not an emergency stop: the mower drives back to its dock."""
        await self._action(device_name, "return_to_dock", self.can_dock,
                           {MammotionWorkMode.RETURNING.value, MammotionWorkMode.CHARGING.value,
                            MammotionWorkMode.READY.value},
                           "cannot return to dock")

    async def cancel(self, device_name: str) -> None:
        await self._action(device_name, "cancel_job", self.can_cancel,
                           {MammotionWorkMode.ONLINE.value, MammotionWorkMode.READY.value,
                            MammotionWorkMode.CHARGING.value},
                           "cannot cancel", "cancel only works when a task is active")

    # === schedules and history ===

    async def get_schedules(self, device_name: str) -> list[Plan]:
        """On-device schedules from fresh correlated replies (never cached plans)."""
        await self._require_mower(device_name)
        try:
            plans: dict[str, Plan] = {}
            index = 0
            expected_total = None
            while True:
                reply = await self._client.send_command_and_wait(
                    device_name, "read_plan", "todev_planjob_set",
                    sub_cmd=2, plan_index=index, send_timeout=10.0, priority=Priority.USER,
                )
                plan = Plan.from_wire(reply.nav.todev_planjob_set, device_name)
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
            return list(plans.values())
        except Exception as e:
            raise MammotionError(f"failed to get schedule: {e}") from e

    async def get_reports(self, device_name: str, count: int = 10, today_only: bool = False) -> list[dict[str, Any]]:
        """Mowing history reports, newest first (raw fields, epoch-second timestamps)."""
        if count <= 0:
            raise MammotionError("report count must be positive")
        await self._require_mower(device_name)
        handle = self._client.mower(device_name)
        work_reports: list[dict] = []
        original_apply = handle._reducer.apply
        query_acked = asyncio.Event()
        all_received = asyncio.Event()
        announced = 0  # total_ack_num: how many records the mower says it is sending

        # wrap the reducer's apply() to intercept work report messages before
        # they're deep-copied and overwrite the previous record — this is the
        # only reliable hook since the reducer deep-copies work_session_result
        # on every toapp_work_report_ack, making subclassing useless
        def _patched_apply(current, message):
            import betterproto2
            nonlocal announced
            nav_msg_name, nav_msg = "", None
            res = betterproto2.which_one_of(message, "LubaSubMsg")
            if res[0] == "nav":
                nav_msg_name, nav_msg = betterproto2.which_one_of(message.nav, "SubNavMsg")
            result = original_apply(current, message)
            if nav_msg_name == "toapp_work_report_update_ack":
                query_acked.set()
            elif nav_msg_name in ("toapp_work_report_ack", "toapp_work_report_upload"):
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
                announced = max(announced, getattr(nav_msg, 'total_ack_num', 0) or 0)
                if len(work_reports) >= (min(count, announced) if announced else count):
                    all_received.set()
            return result

        handle._reducer.apply = _patched_apply
        try:
            await self._client.send_command_with_args(device_name, "query_job_history", priority=Priority.USER)
            # the mower acks the history query before it will answer a request
            try:
                await asyncio.wait_for(query_acked.wait(), timeout=2)
            except TimeoutError:
                pass
            await self._client.send_command_with_args(device_name, "request_job_history",
                                                      priority=Priority.USER, num=count)

            # Done once `count` records, or every record the mower announced
            # (total_ack_num; it may send fewer than requested), has arrived.
            # Without an announcement, fall back to 10s with no new record.
            prev_count = 0
            stale_polls = 0
            for _ in range(120):  # up to 60s total
                try:
                    await asyncio.wait_for(all_received.wait(), timeout=0.5)
                    break
                except TimeoutError:
                    pass
                cur_count = len(work_reports)
                if cur_count > prev_count:
                    stale_polls = 0
                    prev_count = cur_count
                else:
                    stale_polls += 1
                    if stale_polls >= 20 and cur_count > 0:
                        # 10s with no new record — device is done sending
                        break
        except Exception as e:
            raise MammotionError(f"failed to get mow reports: {e}") from e
        finally:
            handle._reducer.apply = original_apply

        work_reports.sort(key=lambda r: r['start_work_time'], reverse=True)
        if today_only:
            today = datetime.now().strftime("%Y-%m-%d")
            work_reports = [r for r in work_reports
                            if datetime.fromtimestamp(r['start_work_time']).strftime("%Y-%m-%d") == today]
        return work_reports

    # === stop ===

    async def stop(self) -> None:
        # A failed restore may leave only a partial session: do not overwrite a good cache.
        if getattr(self, '_authenticated', False):
            self._save_cache()
        try:
            await self._client.stop()
        except Exception:
            pass
        # Finalize orphaned paho clients (e.g. a session replaced by re-login) while the
        # loop is open; their __del__ schedules a callback that fails on a closed loop.
        gc.collect()
        await asyncio.sleep(0)


class MammotionCLI(Mammotion):
    """Command-line front end: formats Mammotion results and maps errors to an exit code."""

    def __init__(self):
        super().__init__()
        self.exit_code = 0

    def _error(self, message: str) -> None:
        self.exit_code = 1
        print(message)

    def check_not_rtk(self, device_name: str) -> bool:
        if self.is_rtk_device(device_name):
            self._error("RTK does not support this command")
            return False
        return True

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
        if getattr(args, 'json', False):
            if not self.check_not_rtk(args.device):
                return
            try:
                state = await self.get_device_state(args.device)
            except MammotionError as e:
                self._error(str(e))
                return
            print(orjson.dumps({'device': args.device, **state}).decode())
            return

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
            try:
                state = await self.get_device_state(args.device)
            except MammotionError as e:
                self._error(str(e))
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
        print(f"starting {args.device}: {', '.join(args.areas)}")
        if all(math.isfinite(v) for v in (args.cutting_height, args.path_spacing)):
            print(f"  pattern: {args.pattern}, spacing: {args.path_spacing}in "
                  f"({int(args.path_spacing * CM_PER_INCH)}cm), perimeter laps: {args.perimeter_laps}")
            print(f"  mow order: {args.mow_order}, speed: {args.speed}, cutting height: {args.cutting_height}in "
                  f"({blade_height_mm(args.cutting_height)}mm), angle: {args.mowing_angle}°")
        try:
            resolved = await self.start_mowing(
                args.device, args.areas,
                pattern=args.pattern,
                cutting_height=args.cutting_height,
                path_spacing=args.path_spacing,
                perimeter_laps=args.perimeter_laps,
                mow_order=args.mow_order,
                mowing_angle=args.mowing_angle,
                speed=args.speed,
            )
        except MammotionError as e:
            self._error(f"error: {e}")
            return
        for area in resolved:
            print(f"  - {area.name} (hash: {area.hash})")
        print(f"started mowing task on {args.device}")

    async def _run_action(self, action, device_name: str, success_msg: str) -> None:
        if not self.check_not_rtk(device_name):
            return
        try:
            await action(device_name)
        except MammotionError as e:
            self._error(str(e))
            return
        print(success_msg)

    async def cmd_pause(self, args) -> None:
        await self._run_action(self.pause, args.device, f"paused {args.device}")

    async def cmd_resume(self, args) -> None:
        await self._run_action(self.resume, args.device, f"resumed mowing on {args.device}")

    async def cmd_return(self, args) -> None:
        await self._run_action(self.return_to_dock, args.device, f"{args.device} returning to dock")

    async def cmd_cancel(self, args) -> None:
        await self._run_action(self.cancel, args.device, f"cancelled task on {args.device}")

    async def cmd_areas(self, args) -> None:
        if not self.check_not_rtk(args.device):
            return

        try:
            areas = await self.get_area_list(args.device)
        except MammotionError as e:
            self._error(str(e))
            return

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

        try:
            await self._require_mower(args.device)
        except MammotionError as e:
            self._error(str(e))
            return

        try:
            areas = await self.get_area_list(args.device)
        except MammotionError as e:
            # Schedules are still worth showing; zones fall back to their hashes.
            self._error(str(e))
            areas = []
        area_map = {area.hash: area.name for area in areas}

        try:
            plans = await self.get_schedules(args.device)
        except MammotionError as e:
            self._error(str(e))
            return

        print(f"\nSchedules for {args.device}:")
        print("=" * 70)

        if not plans:
            print("\nNo scheduled tasks found.")
            print("Schedules can be created in the Mammotion app.")
            return

        # day of week mapping (some devices use 0=Sun, others use 7=Sun)
        day_names = {0: "Sun", 1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}
        pattern_names = {value: name for name, value in PATTERNS.items()}

        for idx, plan in enumerate(plans, 1):
            print(f"\n  [{idx}/{len(plans)}] Schedule: {plan.task_name or plan.job_name or plan.plan_id}")
            print(f"  Enabled:     {'yes' if plan.is_enabled() else 'no'}")

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

    async def cmd_reports(self, args) -> None:
        if not self.check_not_rtk(args.device):
            return

        try:
            work_reports = await self.get_reports(args.device, args.count, today_only=args.today_only)
        except MammotionError as e:
            self._error(str(e))
            return

        if getattr(args, 'json', False):
            print(orjson.dumps(work_reports).decode())
            return

        if not args.content_only:
            print(f"\nMowing History for {args.device}:")
            print("=" * 70)

        if not work_reports:
            print("\nNo mowing reports available")
            return

        for idx, report in enumerate(work_reports, 1):
            print(f"\n  [{idx}/{len(work_reports)}] Mowing Report:")

            # timestamps
            if report['start_work_time'] > 0:
                start = datetime.fromtimestamp(report['start_work_time'])
                print(f"  Started:     {start.strftime('%Y-%m-%d %H:%M:%S')}")

            if report['end_work_time'] > 0:
                end = datetime.fromtimestamp(report['end_work_time'])
                print(f"  Ended:       {end.strftime('%Y-%m-%d %H:%M:%S')}")

            # time on the job: elapsed, cutting, and the rest (mid-job recharging and pauses)
            elapsed, mowing, other = report_times(report)
            if elapsed > 0:
                print(f"  Elapsed:     {format_duration(elapsed)}")
            if mowing > 0:
                print(f"  Mowing:      {format_duration(mowing)}")
            if elapsed > 0:
                print(f"  Charging:    {format_duration(other)} (incl. any pauses)")

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

            # how the job was launched (not a mow order or pattern)
            wt = report['work_type']
            if wt > 0:
                print(f"  Job Type:    {WORK_TYPE_NAMES.get(wt, f'unknown ({wt})')}")

            # result status
            if report['interrupt_flag']:
                print("  Result:      interrupted")
            else:
                wr = report['work_result']
                print(f"  Result:      {WORK_RESULT_NAMES.get(wr, f'unknown ({wr})')}")

            # verbose debug output
            if args.verbose:
                print(f"  [DEBUG] work_type={report['work_type']}, work_result={report['work_result']}")

        if not args.content_only:
            print(f"\n{'=' * 70}")

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
            try:
                await self.login(email, password, use_cache=use_cache)
            except MammotionError as e:
                self._error(str(e))
                return 1

            # Listing and RTK metadata don't require mower MQTT readiness.
            device_name = getattr(args, 'device', None)
            if device_name and not (args.command == 'status' and self.is_rtk_device(device_name)):
                try:
                    await self._require_mower(device_name)
                except MammotionError as e:
                    self._error(f"error: {e}")
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
    status_parser.add_argument('--json', action='store_true', help='print fresh mower state as one JSON object (mowers only)')
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
    reports_parser.add_argument('--json', action='store_true', help='print reports as a JSON array (raw fields, epoch seconds)')
    reports_parser.set_defaults(func=lambda ctl: lambda args: ctl.cmd_reports(args))

    args = parser.parse_args()

    # Library users configure logging themselves; the CLI shows warnings only.
    logging.basicConfig(level=logging.WARNING)

    # Output contains °, ² and similar; Windows pipes otherwise default to cp1252.
    sys.stdout.reconfigure(encoding='utf-8')

    if not args.command:
        parser.print_help()
        return 1

    client = MammotionCLI()

    if hasattr(args, 'func'):
        args.func = args.func(client)

    return run_async(client.run(args))


if __name__ == '__main__':
    sys.exit(main())
