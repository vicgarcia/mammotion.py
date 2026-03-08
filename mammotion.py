#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pymammotion>=0.5.71",
#     "aiohttp>=3.9.0",
# ]
# ///

import argparse
import asyncio
import base64
import json
import logging
import os
import sys
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pymammotion import MammotionHTTP, CloudIOTGateway
from pymammotion.mammotion.commands.mammotion_command import MammotionCommand
from pymammotion.mammotion.devices.mammotion_cloud import MammotionCloud, MammotionBaseCloudDevice
from pymammotion.mqtt import AliyunMQTT
from pymammotion.data.model.device import MowingDevice
from pymammotion.data.mower_state_manager import MowerStateManager
from pymammotion.data.model.generate_route_information import GenerateRouteInformation
from pymammotion.aliyun.model.aep_response import AepResponse
from pymammotion.aliyun.model.connect_response import ConnectResponse
from pymammotion.aliyun.model.dev_by_account_response import ListingDevAccountResponse
from pymammotion.aliyun.model.login_by_oauth_response import LoginByOAuthResponse
from pymammotion.aliyun.model.regions_response import RegionResponse
from pymammotion.aliyun.model.session_by_authcode_response import SessionByAuthCodeResponse
from pymammotion.http.model.http import LoginResponseData, JWTTokenInfo

# setup logging
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# silence noisy mqtt/linkkit loggers (try multiple name variations)
for logger_name in ["Paho", "paho", "paho.mqtt", "linkkit", "aliyunsdkiotx"]:
    logging.getLogger(logger_name).setLevel(logging.CRITICAL)

# conversion constants
MM_PER_INCH = 25.4
CM_PER_INCH = 2.54
METERS_TO_MILES = 0.000621371
SQFT_PER_SQM = 10.764

# auth cache configuration
AUTH_CACHE_DIR = Path.home() / '.mammotion'
AUTH_CACHE_FILE = AUTH_CACHE_DIR / 'auth.json'
TOKEN_REFRESH_BUFFER_SECONDS = 5 * 60  # refresh if expiring within 5 minutes

# suppress mqtt cleanup errors
def mqtt_exception_handler(loop, context):
    """suppress expected mqtt cleanup errors."""
    exception = context.get('exception')
    if exception and isinstance(exception, TypeError):
        if 'DataEvent.data_event()' in str(exception):
            return  # suppress this specific error
    # let other exceptions through
    loop.default_exception_handler(context)


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
        """look up mode by int value, returns None if not found."""
        for mode in cls:
            if mode.value == value:
                return mode
        return None

    @classmethod
    def display_for(cls, value: int) -> str:
        """get display name for an int value, with fallback for unknown values."""
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
        """look up level by int value, returns None if not found."""
        for level in cls:
            if level.value == value:
                return level
        return None

    @classmethod
    def display_for(cls, value: int) -> str:
        """get display name for an int value, with fallback for unknown values."""
        level = cls.from_value(value)
        return level.display if level else f"unknown ({value})"


class MammotionClient:
    """controller for mammotion mower via cloud http api."""

    def __init__(self):
        self.http: MammotionHTTP | None = None
        self.cloud_gateway: CloudIOTGateway | None = None
        self.devices: list[dict[str, Any]] = []
        self.user_account: int = 0

    def is_rtk_device(self, device_name: str) -> bool:
        """check if device is an RTK base station (not a mower)."""
        return device_name.upper().startswith("RTK")

    def check_not_rtk(self, device_name: str) -> bool:
        """check device is not RTK, print message if it is. returns True if OK to proceed."""
        if self.is_rtk_device(device_name):
            print("RTK does not support this command")
            return False
        return True

    @staticmethod
    def _get_attr(obj, *keys, default=''):
        """get attribute from dict or object, trying multiple key names."""
        for key in keys:
            if isinstance(obj, dict):
                if key in obj:
                    return obj[key]
            elif hasattr(obj, key):
                return getattr(obj, key)
        return default

    def _create_mqtt_connection(self) -> MammotionCloud:
        """create mqtt connection for device communication."""
        return MammotionCloud(
            AliyunMQTT(
                region_id=self.cloud_gateway.region_response.data.regionId,
                product_key=self.cloud_gateway.aep_response.data.productKey,
                device_name=self.cloud_gateway.aep_response.data.deviceName,
                device_secret=self.cloud_gateway.aep_response.data.deviceSecret,
                iot_token=self.cloud_gateway.session_by_authcode_response.data.iotToken,
                client_id=self.cloud_gateway.client_id,
                cloud_client=self.cloud_gateway
            ),
            cloud_client=self.cloud_gateway
        )

    def _find_cloud_device(self, device_name: str):
        """find cloud device object by name from cloud gateway response."""
        if not self.cloud_gateway or not self.cloud_gateway.devices_by_account_response:
            return None
        for dev in self.cloud_gateway.devices_by_account_response.data.data:
            if dev.device_name == device_name:
                return dev
        return None

    def _save_auth_cache(self) -> bool:
        """save current auth state to cache file."""
        if not self.http or not self.cloud_gateway:
            return False

        try:
            AUTH_CACHE_DIR.mkdir(parents=True, exist_ok=True)

            cache_data = {
                'version': 1,
                'cached_at': int(time.time()),
                'http': {
                    'login_info': self.http.login_info.to_dict() if self.http.login_info else None,
                    'expires_in': self.http.expires_in,
                    'jwt_info': self.http.jwt_info.to_dict() if self.http.jwt_info else None,
                    'account': self.http.account,
                },
                'cloud_gateway': {
                    'region_response': self.cloud_gateway.region_response.to_dict() if self.cloud_gateway.region_response else None,
                    'connect_response': self.cloud_gateway.connect_response.to_dict() if self.cloud_gateway.connect_response else None,
                    'login_by_oauth_response': self.cloud_gateway.login_by_oauth_response.to_dict() if self.cloud_gateway.login_by_oauth_response else None,
                    'aep_response': self.cloud_gateway.aep_response.to_dict() if self.cloud_gateway.aep_response else None,
                    'session_by_authcode_response': self.cloud_gateway.session_by_authcode_response.to_dict() if self.cloud_gateway.session_by_authcode_response else None,
                    'devices_by_account_response': self.cloud_gateway.devices_by_account_response.to_dict() if self.cloud_gateway.devices_by_account_response else None,
                    'iot_token_issued_at': self.cloud_gateway._iot_token_issued_at,
                    'client_id': self.cloud_gateway._client_id,
                    'device_sn': self.cloud_gateway._device_sn,
                    'utdid': self.cloud_gateway._utdid,
                },
                'user_account': self.user_account,
            }

            AUTH_CACHE_FILE.write_text(json.dumps(cache_data, indent=2))
            logger.debug("saved auth cache to %s", AUTH_CACHE_FILE)
            return True

        except Exception as e:
            logger.warning("failed to save auth cache: %s", e)
            return False

    def _load_auth_cache(self) -> dict | None:
        """load auth cache from file, returns None if invalid or missing."""
        if not AUTH_CACHE_FILE.exists():
            return None

        try:
            cache_data = json.loads(AUTH_CACHE_FILE.read_text())

            # validate version
            if cache_data.get('version') != 1:
                logger.debug("auth cache version mismatch, ignoring")
                return None

            return cache_data

        except Exception as e:
            logger.warning("failed to load auth cache: %s", e)
            return None

    def _is_token_valid(self, cache_data: dict) -> tuple[bool, bool]:
        """check if cached tokens are still valid.

        returns (iot_token_valid, can_refresh) tuple.
        """
        now = int(time.time())

        cg = cache_data.get('cloud_gateway', {})
        session = cg.get('session_by_authcode_response', {})
        session_data = session.get('data', {})
        iot_token_issued_at = cg.get('iot_token_issued_at', 0)
        iot_token_expire = session_data.get('iotTokenExpire', 0)
        refresh_token_expire = session_data.get('refreshTokenExpire', 0)

        # check if iot token is still valid (with buffer)
        iot_token_expiry = iot_token_issued_at + iot_token_expire
        iot_token_valid = iot_token_expiry > (now + TOKEN_REFRESH_BUFFER_SECONDS)

        # check if refresh token is still valid
        refresh_token_expiry = iot_token_issued_at + refresh_token_expire
        can_refresh = refresh_token_expiry > now

        logger.debug("token check: iot_valid=%s, can_refresh=%s, iot_expires_in=%ds, refresh_expires_in=%ds",
                     iot_token_valid, can_refresh,
                     iot_token_expiry - now,
                     refresh_token_expiry - now)

        return iot_token_valid, can_refresh

    async def _restore_from_cache(self, cache_data: dict) -> bool:
        """restore http and cloud_gateway state from cache data."""
        try:
            http_data = cache_data.get('http', {})
            cg_data = cache_data.get('cloud_gateway', {})

            # restore http client
            self.http = MammotionHTTP()
            if http_data.get('login_info'):
                self.http.login_info = LoginResponseData.from_dict(http_data['login_info'])
            self.http.expires_in = http_data.get('expires_in', 0)
            if http_data.get('jwt_info'):
                self.http.jwt_info = JWTTokenInfo.from_dict(http_data['jwt_info'])
            self.http.account = http_data.get('account')
            if self.http.login_info:
                self.http._headers["Authorization"] = f"Bearer {self.http.login_info.access_token}"
                # The library expects response.data to contain userInformation
                # Create a simple wrapper so mqtt.cloud_client.mammotion_http.response.data works
                class ResponseWrapper:
                    def __init__(self, data):
                        self.data = data
                self.http.response = ResponseWrapper(self.http.login_info)

            # restore cloud gateway with all cached responses
            self.cloud_gateway = CloudIOTGateway(
                mammotion_http=self.http,
                connect_response=ConnectResponse.from_dict(cg_data['connect_response']) if cg_data.get('connect_response') else None,
                login_by_oauth_response=LoginByOAuthResponse.from_dict(cg_data['login_by_oauth_response']) if cg_data.get('login_by_oauth_response') else None,
                aep_response=AepResponse.from_dict(cg_data['aep_response']) if cg_data.get('aep_response') else None,
                session_by_authcode_response=SessionByAuthCodeResponse.from_dict(cg_data['session_by_authcode_response']) if cg_data.get('session_by_authcode_response') else None,
                region_response=RegionResponse.from_dict(cg_data['region_response']) if cg_data.get('region_response') else None,
                dev_by_account=ListingDevAccountResponse.from_dict(cg_data['devices_by_account_response']) if cg_data.get('devices_by_account_response') else None,
            )

            # restore hardware identifiers
            if cg_data.get('client_id'):
                self.cloud_gateway._client_id = cg_data['client_id']
            if cg_data.get('device_sn'):
                self.cloud_gateway._device_sn = cg_data['device_sn']
            if cg_data.get('utdid'):
                self.cloud_gateway._utdid = cg_data['utdid']
            if cg_data.get('iot_token_issued_at'):
                self.cloud_gateway._iot_token_issued_at = cg_data['iot_token_issued_at']

            # restore user account
            self.user_account = cache_data.get('user_account', 0)

            return True

        except Exception as e:
            logger.warning("failed to restore from cache: %s", e)
            return False

    async def _try_cached_login(self) -> bool:
        """attempt to login using cached tokens. returns True on success."""
        cache_data = self._load_auth_cache()
        if not cache_data:
            logger.debug("no auth cache found")
            return False

        iot_valid, can_refresh = self._is_token_valid(cache_data)

        if not iot_valid and not can_refresh:
            logger.debug("cached tokens expired and cannot refresh")
            return False

        # restore state from cache
        if not await self._restore_from_cache(cache_data):
            return False

        # if iot token expired but can refresh, do so
        if not iot_valid and can_refresh:
            logger.debug("iot token expired, refreshing...")
            try:
                await self.cloud_gateway.check_or_refresh_session()
                # update issued timestamp
                self.cloud_gateway._iot_token_issued_at = int(time.time())
                # save refreshed tokens
                self._save_auth_cache()
                print("refreshed cached session...")
            except Exception as e:
                logger.warning("token refresh failed: %s", e)
                return False
        else:
            print("using cached session...")

        return True

    async def login(self, email: str, password: str, use_cache: bool = True) -> bool:
        """login to mammotion cloud and setup http client."""
        # try cached login first
        if use_cache:
            if await self._try_cached_login():
                return True
            logger.debug("cached login failed, doing fresh login")

        try:
            self.http = MammotionHTTP()

            # login via http
            login_resp = await self.http.login_v2(email, password)
            if not login_resp or login_resp.code != 0:
                print(f"login failed: {login_resp.msg if login_resp else 'unknown error'}")
                return False

            # get user account id
            self.user_account = int(self.http.login_info.userInformation.userAccount)

            # setup cloud gateway
            self.cloud_gateway = CloudIOTGateway(self.http)
            await self.cloud_gateway.connect()
            await self.cloud_gateway.get_region("US")
            await self.cloud_gateway.login_by_oauth("US")
            await self.cloud_gateway.aep_handle()
            await self.cloud_gateway.session_by_auth_code()
            await self.cloud_gateway.list_binding_by_account()

            # save auth cache for next time
            self._save_auth_cache()

            return True

        except Exception as e:
            logger.exception("login error")
            print(f"login failed: {e}")
            return False

    async def get_devices(self) -> list[dict[str, Any]] | None:
        """get list of devices from cloud. returns None on failure."""
        if not self.http or not self.cloud_gateway:
            return None

        resp = await self.http.get_user_device_list()
        if not resp or resp.code != 0:
            return None

        devices = []
        for dev in resp.data:
            dev_name = self._get_attr(dev, 'device_name', 'deviceName')
            iot_id = self._get_attr(dev, 'iot_id', 'iotId')

            # get product_key from cloud_gateway devices_by_account response
            cloud_dev = self._find_cloud_device(dev_name)
            product_key = cloud_dev.product_key if cloud_dev else ''

            devices.append({
                'device_name': dev_name,
                'iot_id': iot_id,
                'product_key': product_key,
            })

        self.devices = devices
        return devices

    async def close(self):
        """close http session."""
        if self.http and self.http._session:
            await self.http._session.close()

    def find_device(self, device_name: str) -> dict[str, Any] | None:
        """find device by name."""
        for dev in self.devices:
            if dev['device_name'] == device_name:
                return dev
        return None

    async def send_command(self, device_name: str, command_bytes: bytes) -> bool:
        """send protobuf command via http mqtt_invoke."""
        if not self.http:
            print("not logged in")
            return False

        device = self.find_device(device_name)
        if not device:
            print(f"device not found: {device_name}")
            return False

        # encode command as base64
        content = base64.b64encode(command_bytes).decode('utf-8')

        # send via http rpc
        try:
            resp = await self.http.mqtt_invoke(
                content=content,
                device_name=device['device_name'],
                iot_id=device['iot_id']
            )

            if resp.code != 0:
                print(f"command failed: {resp.msg}")
                return False
            return True

        except Exception as e:
            logger.exception("send_command error")
            print(f"command failed: {e}")
            return False

    def create_command(self, device_name: str) -> MammotionCommand:
        """create command builder for device."""
        cmd = MammotionCommand(device_name, self.user_account)

        # set product_key for proper device type detection
        device = self.find_device(device_name)
        if device and device.get('product_key'):
            cmd.set_device_product_key(device['product_key'])

        return cmd

    def can_pause(self, status: int) -> bool:
        """check if device can be paused."""
        return status == MammotionWorkMode.WORKING.value

    def can_resume(self, status: int) -> bool:
        """check if device can be resumed."""
        return status in (MammotionWorkMode.PAUSE.value, MammotionWorkMode.CHARGING_PAUSE.value)

    def can_cancel(self, status: int) -> bool:
        """check if there's an active task to cancel."""
        return status in (
            MammotionWorkMode.WORKING.value,
            MammotionWorkMode.PAUSE.value,
            MammotionWorkMode.CHARGING_PAUSE.value,
            MammotionWorkMode.RETURNING.value,
        )

    def can_dock(self, status: int) -> bool:
        """check if device can be sent to dock."""
        return status in (
            MammotionWorkMode.READY.value,
            MammotionWorkMode.WORKING.value,
            MammotionWorkMode.PAUSE.value,
        )

    async def get_device_state(self, device_name: str) -> dict[str, Any] | None:
        """get current device state via mqtt."""
        # set exception handler to suppress mqtt cleanup errors
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(mqtt_exception_handler)

        device = self.find_device(device_name)
        if not device:
            return None

        try:
            mqtt = self._create_mqtt_connection()
            cloud_dev = self._find_cloud_device(device['device_name'])
            if not cloud_dev:
                return None

            state_manager = MowerStateManager(MowingDevice())
            cloud_device = MammotionBaseCloudDevice(
                mqtt=mqtt,
                cloud_device=cloud_dev,
                state_manager=state_manager
            )

            mqtt.connect_async()
            await asyncio.sleep(2)

            # request fresh state data
            await cloud_device.queue_command("get_report_cfg")
            await asyncio.sleep(2)

            # get state from state manager
            device_obj = state_manager.get_device()

            # progress and time are bit-packed in the work fields
            area_raw = device_obj.report_data.work.area
            progress_raw = device_obj.report_data.work.progress

            # get position from locations list if available
            pos_x, pos_y, heading = 0, 0, 0
            if device_obj.report_data.locations:
                loc = device_obj.report_data.locations[0]
                pos_x = loc.real_pos_x
                pos_y = loc.real_pos_y
                heading = loc.real_toward

            state = {
                'status': device_obj.report_data.dev.sys_status,
                'status_name': MammotionWorkMode.display_for(device_obj.report_data.dev.sys_status),
                'battery': device_obj.report_data.dev.battery_val,
                'progress': area_raw >> 16,  # upper 16 bits = progress %
                'total_time_min': progress_raw & 65535,  # lower 16 bits = total time in minutes
                'time_left_min': progress_raw >> 16,  # upper 16 bits = time left in minutes
                'pos_x': pos_x,
                'pos_y': pos_y,
                'heading': heading,
                'blade_height': device_obj.report_data.work.knife_height,
                'gps_stars': device_obj.report_data.rtk.gps_stars,
                'co_view_stars': device_obj.report_data.rtk.co_view_stars,
                'rtk_status': device_obj.report_data.rtk.status,
                'rtk_pos_level': device_obj.report_data.rtk.pos_level,
                'rtk_dis_status': device_obj.report_data.rtk.dis_status,
                'lifetime_hours': device_obj.report_data.maintenance.work_time,
                'mileage': device_obj.report_data.maintenance.mileage,
            }

            # cleanup
            mqtt.disconnect()

            return state

        except Exception as e:
            logger.exception("get_device_state error")
            return None

    async def get_area_list(self, device_name: str) -> list[Any]:
        """get list of areas from device via mqtt."""
        # set exception handler to suppress mqtt cleanup errors
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(mqtt_exception_handler)

        device = self.find_device(device_name)
        if not device:
            return []

        mqtt = self._create_mqtt_connection()
        cloud_dev = self._find_cloud_device(device['device_name'])
        if not cloud_dev:
            return []

        state_manager = MowerStateManager(MowingDevice())
        cloud_device = MammotionBaseCloudDevice(
            mqtt=mqtt,
            cloud_device=cloud_dev,
            state_manager=state_manager
        )

        mqtt.connect_async()
        await asyncio.sleep(3)
        await cloud_device.queue_command("send_todev_ble_sync", sync_type=3)
        await asyncio.sleep(1)
        await cloud_device.queue_command("get_area_name_list", device_id=device['iot_id'])
        await asyncio.sleep(1)
        await cloud_device.queue_command("get_all_boundary_hash_list", sub_cmd=0)

        # poll for areas
        max_retries = 10
        for _ in range(max_retries):
            await asyncio.sleep(2)
            device_obj = state_manager.get_device()
            if device_obj.map.area_name:
                break

        # get final area list
        device_obj = state_manager.get_device()
        areas = list(device_obj.map.area_name) if device_obj.map.area_name else []

        # cleanup
        mqtt.disconnect()

        return areas
    # === command handlers ===

    async def cmd_devices(self, args) -> None:
        """list all devices."""
        devices = await self.get_devices()

        print("\nDevices:")
        print("=" * 70)

        if not devices:
            print("\nNo devices found.")
        else:
            for dev in devices:
                print(f"  {dev['device_name']}")
            print(f"\n{'=' * 70}")
            print(f"Total: {len(devices)} device(s)")

    async def cmd_status(self, args) -> None:
        """show device status."""
        # handle RTK devices differently
        if self.is_rtk_device(args.device):
            await self.cmd_status_rtk(args)
            return

        print("retrieving device status...")
        state = await self.get_device_state(args.device)

        if not state:
            print("failed to get device status")
            return

        print(f"\nStatus for {args.device}:")
        print("=" * 70)
        print(f"  Status: {state['status_name']}")
        print(f"  Battery: {state['battery']}%")

        # show progress if mowing or paused
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

        # show position if available
        if state['pos_x'] != 0 or state['pos_y'] != 0:
            # convert mm to meters for display
            x_m = state['pos_x'] / 1000
            y_m = state['pos_y'] / 1000
            heading_deg = (state['heading'] / 100) % 360

            print(f"  Position: ({x_m:.1f}m, {y_m:.1f}m) heading {heading_deg:.0f}°")

        # show blade height if relevant
        if state['blade_height'] > 0:
            blade_height_in = state['blade_height'] / MM_PER_INCH
            print(f"  Blade height: {state['blade_height']}mm ({blade_height_in:.1f}in)")

        # rtk/gps status
        if state['gps_stars'] > 0:
            rtk_level = MammotionRtkLevel.display_for(state['rtk_pos_level'])
            print(f"  RTK: {rtk_level} | GPS: {state['gps_stars']} satellites")

        # maintenance stats
        if state['lifetime_hours'] > 0:
            hours = state['lifetime_hours'] // 3600
            print(f"  Lifetime work time: {hours}h")
        if state['mileage'] > 0:
            miles = state['mileage'] * METERS_TO_MILES
            print(f"  Total mileage: {miles:.1f} miles")

        print("=" * 70)

    async def cmd_status_rtk(self, args) -> None:
        """show RTK base station status."""
        # get device info from cloud gateway
        cloud_dev = None
        for dev in self.cloud_gateway.devices_by_account_response.data.data:
            if dev.device_name == args.device:
                cloud_dev = dev
                break

        if not cloud_dev:
            print(f"device not found: {args.device}")
            return

        print(f"\nStatus for {args.device}:")
        print("=" * 70)
        print(f"  Type: RTK Base Station")
        print(f"  Status: {'online' if cloud_dev.status == 1 else 'offline'}")
        if cloud_dev.product_name:
            print(f"  Product: {cloud_dev.product_name}")
        if cloud_dev.product_model:
            print(f"  Model: {cloud_dev.product_model}")
        print("=" * 70)

    async def cmd_start(self, args) -> None:
        """start mowing task with specified areas."""
        if not self.check_not_rtk(args.device):
            return

        # set exception handler to suppress mqtt cleanup errors
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(mqtt_exception_handler)

        # validate inputs
        if args.speed < 0.0 or args.speed > 1.0:
            print(f"error: speed must be between 0.0 and 1.0 (got {args.speed})")
            return

        if args.cutting_height < 2.2 or args.cutting_height > 3.9:
            print(f"error: cutting height must be between 2.2in and 3.9in (got {args.cutting_height}in)")
            return

        if args.path_spacing < 7.9 or args.path_spacing > 13.8:
            print(f"error: path spacing must be between 7.9in and 13.8in (got {args.path_spacing}in)")
            return

        if args.perimeter_laps < 0 or args.perimeter_laps > 4:
            print(f"error: perimeter laps must be between 0 and 4 (got {args.perimeter_laps})")
            return

        if args.mowing_angle < 0 or args.mowing_angle > 359:
            print(f"error: mowing angle must be between 0 and 359 degrees (got {args.mowing_angle})")
            return

        # convert pattern string to channel_mode int
        pattern_map = {'perimeter': 3, 'zigzag': 0, 'chessboard': 1, 'adaptive': 2}
        channel_mode = pattern_map[args.pattern]

        # convert mow_order to border_mode (0=perimeter first, 1=grid first)
        border_mode = 0 if args.mow_order == 'perimeter-first' else 1

        # convert inches to millimeters/centimeters for api
        blade_height_mm = int(args.cutting_height * MM_PER_INCH)
        path_spacing_cm = int(args.path_spacing * CM_PER_INCH)

        # get areas from device
        print("fetching areas...")
        areas = await self.get_area_list(args.device)
        if not areas:
            print("failed to get areas - cannot start task")
            return

        # resolve area names/hashes from arguments
        area_hashes = []
        for area_input in args.areas:
            # try to match by name or hash
            matched = False
            for area in areas:
                if area.name == area_input or str(area.hash) == area_input:
                    area_hashes.append(area.hash)
                    matched = True
                    print(f"  - {area.name} (hash: {area.hash})")
                    break

            if not matched:
                print(f"error: area '{area_input}' not found")
                print(f"available areas: {', '.join([a.name for a in areas])}")
                return

        if not area_hashes:
            print("error: no valid areas specified")
            return

        # build path_order byte string (encodes border_mode and other settings)
        path_order_bytes = bytearray(8)
        path_order_bytes[0] = border_mode  # 0=perimeter first, 1=grid first
        path_order_bytes[1] = 1  # obstacle_laps
        path_order_bytes[2] = 0
        path_order_bytes[3] = 0  # start_progress
        path_order_bytes[4] = 0
        path_order_bytes[5] = 0
        path_order_bytes[6] = 10  # collect_grass_frequency (not used for luba)
        path_order_bytes[7] = 0
        path_order = path_order_bytes.decode('latin-1')

        print(f"\ngenerating route for {len(area_hashes)} area(s)...")
        print(f"  pattern: {args.pattern}, spacing: {args.path_spacing}in ({path_spacing_cm}cm), perimeter laps: {args.perimeter_laps}")
        print(f"  mow order: {args.mow_order}, speed: {args.speed}, cutting height: {args.cutting_height}in ({blade_height_mm}mm), angle: {args.mowing_angle}°")

        # build route configuration
        route_info = GenerateRouteInformation(
            one_hashs=area_hashes,
            speed=args.speed,
            blade_height=blade_height_mm,
            ultra_wave=2,  # less touch obstacle detection
            channel_mode=channel_mode,
            channel_width=path_spacing_cm,
            edge_mode=args.perimeter_laps,
            job_mode=4,  # standard task mode
            toward=args.mowing_angle,
            toward_included_angle=0,
            toward_mode=1,  # 1 = absolute angle
            path_order=path_order,
        )

        # setup mqtt connection to send commands
        device = self.find_device(args.device)
        if not device:
            print(f"device not found: {args.device}")
            return

        try:
            mqtt = self._create_mqtt_connection()
            cloud_dev = self._find_cloud_device(device['device_name'])
            if not cloud_dev:
                print(f"cloud device not found for {device['device_name']}")
                return

            state_manager = MowerStateManager(MowingDevice())
            cloud_device = MammotionBaseCloudDevice(
                mqtt=mqtt,
                cloud_device=cloud_dev,
                state_manager=state_manager
            )

            mqtt.connect_async()
            await asyncio.sleep(2)

            # send generate route command
            await cloud_device.queue_command("generate_route_information", generate_route_information=route_info)
            await asyncio.sleep(2)

            # start job
            await cloud_device.queue_command("start_job")
            await asyncio.sleep(1)

            print(f"✓ started mowing task on {args.device}")

        except Exception as e:
            logger.exception("start command error")
            print(f"start command failed: {e}")
        finally:
            # disconnect and suppress cleanup errors
            try:
                mqtt.disconnect()
            except Exception:
                pass

    async def cmd_pause(self, args) -> None:
        """pause current job."""
        if not self.check_not_rtk(args.device):
            return

        # check current state
        print("checking device status...")
        state = await self.get_device_state(args.device)
        if not state:
            print("failed to get device status")
            return

        if not self.can_pause(state['status']):
            print(f"cannot pause: device is {state['status_name']}")
            print("pause only works when mowing is in progress")
            return

        cmd = self.create_command(args.device)
        command_bytes = cmd.pause_execute_task()

        if await self.send_command(args.device, command_bytes):
            print(f"✓ paused {args.device}")
        else:
            print("pause command failed")

    async def cmd_resume(self, args) -> None:
        """resume paused job."""
        if not self.check_not_rtk(args.device):
            return

        # check current state
        print("checking device status...")
        state = await self.get_device_state(args.device)
        if not state:
            print("failed to get device status")
            return

        if not self.can_resume(state['status']):
            print(f"cannot resume: device is {state['status_name']}")
            print("resume only works when mowing is paused")
            return

        cmd = self.create_command(args.device)
        command_bytes = cmd.start_job()

        if await self.send_command(args.device, command_bytes):
            print(f"✓ resumed mowing on {args.device}")
        else:
            print("resume command failed")

    async def cmd_return(self, args) -> None:
        """return to dock."""
        if not self.check_not_rtk(args.device):
            return

        # check current state
        print("checking device status...")
        state = await self.get_device_state(args.device)
        if not state:
            print("failed to get device status")
            return

        if not self.can_dock(state['status']):
            print(f"cannot return to dock: device is {state['status_name']}")
            return

        cmd = self.create_command(args.device)
        command_bytes = cmd.return_to_dock()

        if await self.send_command(args.device, command_bytes):
            print(f"✓ {args.device} returning to dock")
        else:
            print("return command failed")

    async def cmd_cancel(self, args) -> None:
        """cancel current job."""
        if not self.check_not_rtk(args.device):
            return

        # check current state
        print("checking device status...")
        state = await self.get_device_state(args.device)
        if not state:
            print("failed to get device status")
            return

        if not self.can_cancel(state['status']):
            print(f"cannot cancel: device is {state['status_name']}")
            print("cancel only works when a task is active")
            return

        cmd = self.create_command(args.device)
        command_bytes = cmd.cancel_job()

        if await self.send_command(args.device, command_bytes):
            print(f"✓ cancelled task on {args.device}")
        else:
            print("cancel command failed")

    async def cmd_areas(self, args) -> None:
        """list all areas/zones (requires MQTT)."""
        if not self.check_not_rtk(args.device):
            return

        print("setting up mqtt connection to retrieve areas...")

        # setup mqtt for this device
        device = self.find_device(args.device)
        if not device:
            print(f"device not found: {args.device}")
            return

        try:
            mqtt = self._create_mqtt_connection()
            cloud_dev = self._find_cloud_device(device['device_name'])
            if not cloud_dev:
                print(f"cloud device not found for {device['device_name']}")
                return

            state_manager = MowerStateManager(MowingDevice())
            cloud_device = MammotionBaseCloudDevice(
                mqtt=mqtt,
                cloud_device=cloud_dev,
                state_manager=state_manager
            )

            mqtt.connect_async()
            await asyncio.sleep(3)

            # sync device state first
            await cloud_device.queue_command("send_todev_ble_sync", sync_type=3)
            await asyncio.sleep(1)

            # request area list
            await cloud_device.queue_command("get_area_name_list", device_id=device['iot_id'])
            await asyncio.sleep(1)

            # get boundary hash list (triggers map load)
            await cloud_device.queue_command("get_all_boundary_hash_list", sub_cmd=0)

            # poll for map data to load
            print("waiting for map data...")
            max_retries = 10
            for _ in range(max_retries):
                await asyncio.sleep(2)
                device_obj = state_manager.get_device()
                if device_obj.map.area_name:
                    break

            # get areas from state manager
            device_obj = state_manager.get_device()

            print(f"\nAreas for {args.device}:")
            print("=" * 70)

            if device_obj.map.area_name:
                for area in device_obj.map.area_name:
                    print(f"  {area.name} (hash: {area.hash})")
                print(f"\n{'=' * 70}")
                print(f"Total: {len(device_obj.map.area_name)} area(s)")
            else:
                print("\nNo areas found.")
                print("Try running again or check if map exists in the app.")

            # disconnect
            mqtt.disconnect()

        except Exception as e:
            logger.exception("areas command error")
            print(f"failed to get areas: {e}")

    async def cmd_schedules(self, args) -> None:
        """list scheduled mowing tasks."""
        if not self.check_not_rtk(args.device):
            return

        # set exception handler to suppress mqtt cleanup errors
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(mqtt_exception_handler)

        device = self.find_device(args.device)
        if not device:
            print(f"device not found: {args.device}")
            return

        try:
            # get area names first for mapping zone hashes
            print("retrieving area names...")
            areas = await self.get_area_list(args.device)
            area_map = {area.hash: area.name for area in areas} if areas else {}

            print("connecting to retrieve schedules...")
            mqtt = self._create_mqtt_connection()
            cloud_dev = self._find_cloud_device(device['device_name'])
            if not cloud_dev:
                print(f"cloud device not found for {device['device_name']}")
                return

            state_manager = MowerStateManager(MowingDevice())
            cloud_device = MammotionBaseCloudDevice(
                mqtt=mqtt,
                cloud_device=cloud_dev,
                state_manager=state_manager
            )

            mqtt.connect_async()
            await asyncio.sleep(2)

            # request plan data - sub_cmd=2 reads plans, plan_index=0 starts from first
            print("requesting schedule data...")
            await cloud_device.queue_command("read_plan", sub_cmd=2, plan_index=0)

            # poll for plans to arrive
            max_retries = 10
            for _ in range(max_retries):
                await asyncio.sleep(2)
                device_obj = state_manager.get_device()
                plans = device_obj.map.plan
                if plans:
                    # check if we have all plans
                    first_plan = list(plans.values())[0] if plans else None
                    if first_plan and first_plan.total_plan_num == len(plans):
                        break
                    # request next plan if more exist
                    if first_plan and len(plans) < first_plan.total_plan_num:
                        await cloud_device.queue_command("read_plan", sub_cmd=2, plan_index=len(plans))

            # get final plans
            device_obj = state_manager.get_device()
            plans = device_obj.map.plan

            # disconnect
            mqtt.disconnect()

            # display schedules
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
                    print(f"\n[{idx}/{len(plans)}] Schedule: {plan.task_name or plan.job_name or plan_id}")

                    # time range
                    if plan.start_time:
                        print(f"  Start time:  {plan.start_time}")
                    if plan.end_time:
                        print(f"  End time:    {plan.end_time}")

                    # date range
                    if plan.start_date:
                        print(f"  Start date:  {plan.start_date}")
                    if plan.end_date:
                        print(f"  End date:    {plan.end_date}")

                    # days of week
                    if plan.weeks:
                        days = [day_names.get(d, str(d)) for d in plan.weeks]
                        print(f"  Days:        {', '.join(days)}")
                    elif plan.week:
                        # single day
                        print(f"  Day:         {day_names.get(plan.week, str(plan.week))}")

                    # zones/areas
                    if plan.zone_hashs:
                        zone_names = []
                        for zh in plan.zone_hashs:
                            if zh in area_map:
                                zone_names.append(area_map[zh])
                            else:
                                zone_names.append(f"hash:{zh}")
                        print(f"  Areas:       {', '.join(zone_names)}")

                    # mowing settings
                    if plan.knife_height > 0:
                        height_in = plan.knife_height / MM_PER_INCH
                        print(f"  Blade:       {plan.knife_height}mm ({height_in:.1f}\")")

                    if plan.route_model >= 0:
                        pattern = pattern_names.get(plan.route_model, f"mode {plan.route_model}")
                        print(f"  Pattern:     {pattern}")

                    if plan.route_spacing > 0:
                        spacing_in = plan.route_spacing / CM_PER_INCH
                        print(f"  Spacing:     {plan.route_spacing}cm ({spacing_in:.1f}\")")

                    if plan.speed > 0:
                        print(f"  Speed:       {plan.speed}")

                    if plan.edge_mode > 0:
                        print(f"  Border laps: {plan.edge_mode}")

                    # IDs for reference
                    if args.verbose:
                        print(f"  [DEBUG] plan_id={plan.plan_id}, task_id={plan.task_id}")
                        print(f"  [DEBUG] work_time={plan.work_time}, required_time={plan.required_time}, area={plan.area}")

                print(f"\n{'=' * 70}")
                print(f"Total: {len(plans)} scheduled task(s)")

        except Exception as e:
            logger.exception("schedules command error")
            print(f"failed to get schedules: {e}")

    async def cmd_reports(self, args) -> None:
        """get mowing job history reports."""
        if not self.check_not_rtk(args.device):
            return

        # set exception handler to suppress mqtt cleanup errors
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(mqtt_exception_handler)

        device = self.find_device(args.device)
        if not device:
            print(f"device not found: {args.device}")
            return

        # storage for work reports
        work_reports = []
        reports_received = asyncio.Event()

        # callback to capture work reports from MQTT messages
        async def capture_work_reports(res: tuple[str, Any]):
            try:
                import betterproto2
                # res is a tuple of (submsg_type, submsg_data) from which_one_of(message, "LubaSubMsg")
                msg_type, nav_msg_obj = res

                # check if this is a nav message
                if msg_type == "nav" and nav_msg_obj:
                    # now parse the nav submessage to see what kind it is
                    nav_msg = betterproto2.which_one_of(nav_msg_obj, "SubNavMsg")
                    if nav_msg[0] in ("toapp_work_report_ack", "toapp_work_report_upload"):
                        report = nav_msg[1]
                        work_reports.append(report)
                        # check if we got all reports
                        if report.current_ack_num == report.total_ack_num and report.current_ack_num > 0:
                            reports_received.set()
            except Exception as e:
                logger.debug(f"error parsing work report: {e}")

        try:
            mqtt = self._create_mqtt_connection()
            cloud_dev = self._find_cloud_device(device['device_name'])
            if not cloud_dev:
                print(f"cloud device not found for {device['device_name']}")
                return

            state_manager = MowerStateManager(MowingDevice())
            cloud_device = MammotionBaseCloudDevice(
                mqtt=mqtt,
                cloud_device=cloud_dev,
                state_manager=state_manager
            )

            # add our custom callback to capture work reports
            state_manager.cloud_on_notification_callback.add_subscribers(capture_work_reports)

            mqtt.connect_async()
            await asyncio.sleep(2)

            # query if history is available
            print("querying work history...")
            await cloud_device.queue_command("query_job_history")
            await asyncio.sleep(2)

            # request work history records
            num_reports = args.count if args.count else 10
            print(f"requesting {num_reports} work report(s)...")
            await cloud_device.queue_command("request_job_history", num=num_reports)

            # wait for all reports to arrive (with timeout)
            try:
                await asyncio.wait_for(reports_received.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.debug("timeout waiting for all reports")

            # give a bit more time for any stragglers
            await asyncio.sleep(1)

            # disconnect
            mqtt.disconnect()

            # Display the reports
            print(f"\nMowing History for {args.device}:")
            print("=" * 70)

            if not work_reports:
                print("\nNo mowing reports available.")
                print("The device may not have any completed mowing sessions yet.")
            else:
                # sort by start time (newest first)
                work_reports.sort(key=lambda r: r.start_work_time, reverse=True)

                for idx, report in enumerate(work_reports, 1):
                    print(f"\n[{idx}/{len(work_reports)}] Mowing Report:")

                    # timestamps
                    if report.start_work_time > 0:
                        start = datetime.fromtimestamp(report.start_work_time)
                        print(f"  Started:     {start.strftime('%Y-%m-%d %H:%M:%S')}")

                    if report.end_work_time > 0:
                        end = datetime.fromtimestamp(report.end_work_time)
                        print(f"  Ended:       {end.strftime('%Y-%m-%d %H:%M:%S')}")

                    # duration
                    if report.work_time_used > 0:
                        hours = report.work_time_used // 3600
                        minutes = (report.work_time_used % 3600) // 60
                        print(f"  Duration:    {hours}h {minutes}m")

                    # area
                    if report.work_ares > 0:
                        sqft = report.work_ares * SQFT_PER_SQM
                        print(f"  Area:        {report.work_ares:.1f} m² ({sqft:.0f} ft²)")

                    # blade height
                    if report.height_of_knife > 0:
                        inches = report.height_of_knife / MM_PER_INCH
                        print(f"  Blade:       {report.height_of_knife}mm ({inches:.1f}\")")

                    # progress
                    if report.work_progress > 0:
                        print(f"  Progress:    {report.work_progress}%")

                    # work type and result
                    work_type_names = {
                        0: "Unknown",
                        1: "Mowing",
                        2: "Border First",
                        3: "Border Only",
                        4: "Task Mode",
                        8: "Manual Mode"
                    }
                    if report.work_type > 0:
                        work_type_str = work_type_names.get(report.work_type, f"Type {report.work_type}")
                        print(f"  Work Type:   {work_type_str}")

                    # result status
                    if report.interrupt_flag:
                        print(f"  Result:      Interrupted")
                    else:
                        # work_result mappings based on observed data patterns
                        result_names = {
                            0: "In Progress",
                            1: "Failed",
                            2: "Canceled",
                            3: "Stopped",      # incomplete, manually stopped or error
                            4: "Paused",
                            5: "Completed",    # successfully finished
                        }
                        result_str = result_names.get(report.work_result, f"Unknown ({report.work_result})")
                        print(f"  Result:      {result_str}")

                    # verbose debug output
                    if args.verbose:
                        print(f"  [DEBUG] work_type={report.work_type}, work_result={report.work_result}, job_content={report.job_content}")

                print(f"\n{'=' * 70}")
                print(f"Total: {len(work_reports)} mowing session(s)")

        except Exception as e:
            logger.exception("reports command error")
            print(f"failed to get mow reports: {e}")

    async def run(self, args) -> int:
        """main run method."""
        try:
            # get credentials from env or args
            email = args.email or os.environ.get('MAMMOTION_EMAIL')
            password = args.password or os.environ.get('MAMMOTION_PASSWORD')

            if not email or not password:
                print("error: email and password required (via args or MAMMOTION_EMAIL/MAMMOTION_PASSWORD env vars)")
                return 1

            # login (use cache unless --no-cache specified)
            use_cache = not getattr(args, 'no_cache', False)
            if not await self.login(email, password, use_cache=use_cache):
                return 1

            # get devices
            devices = await self.get_devices()
            if devices is None:
                print("failed to get devices")
                return 1

            # run command
            if hasattr(args, 'func'):
                await args.func(args)
                return 0

            return 0
        finally:
            # always close session
            await self.close()


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
    start_parser.add_argument('--areas', required=True, nargs='+', help='space-separated area names or hashes to mow')
    start_parser.add_argument('--pattern', type=str, default='zigzag', choices=['perimeter', 'zigzag', 'chessboard', 'adaptive'], help='mowing path pattern: perimeter=perimeter only, zigzag=single pass (default), chessboard=cross/chess pattern, adaptive=adaptive zigzag')
    start_parser.add_argument('--cutting-height', type=float, default=2.8, help='cutting height in inches (2.2-3.9in), default: 2.8in')
    start_parser.add_argument('--path-spacing', type=float, default=10.0, help='spacing between mowing paths in inches (7.9-13.8in), default: 10.0in')
    start_parser.add_argument('--perimeter-laps', type=int, default=2, help='number of border/perimeter laps (0-4), default: 2')
    start_parser.add_argument('--mow-order', type=str, default='grid-first', choices=['perimeter-first', 'grid-first'], help='mowing order: perimeter-first=border then zigzag, grid-first=zigzag then border (default)')
    start_parser.add_argument('--mowing-angle', type=int, default=0, help='mowing angle in degrees (0-359), controls direction of mowing lines, default: 0 (east/west)')
    start_parser.add_argument('--speed', type=float, default=0.5, help='mowing speed: 0.0 (slow) to 1.0 (fast), default: 0.5')
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

    # schedules command
    schedules_parser = subparsers.add_parser('schedules', help='list scheduled mowing tasks')
    schedules_parser.add_argument('--device', required=True, help='device name')
    schedules_parser.add_argument('--verbose', '-v', action='store_true', help='show additional debugging information')
    schedules_parser.set_defaults(func=lambda ctl: lambda args: ctl.cmd_schedules(args))

    # reports command
    reports_parser = subparsers.add_parser('reports', help='get mowing job history reports')
    reports_parser.add_argument('--device', required=True, help='device name')
    reports_parser.add_argument('--count', type=int, default=10, help='number of reports to retrieve (default: 10)')
    reports_parser.add_argument('--verbose', '-v', action='store_true', help='show additional debugging information')
    reports_parser.set_defaults(func=lambda ctl: lambda args: ctl.cmd_reports(args))

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    # create client and run
    client = MammotionClient()

    # bind client to func
    if hasattr(args, 'func'):
        args.func = args.func(client)

    return asyncio.run(client.run(args))


if __name__ == '__main__':
    sys.exit(main())
