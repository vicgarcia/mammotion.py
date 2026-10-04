"""Offline regression tests for the standalone CLI (no login or mower traffic)."""

import asyncio
import importlib.util
import inspect
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch
from contextlib import redirect_stdout


SCRIPT = Path(__file__).resolve().parents[1] / "mammotion.py"
spec = importlib.util.spec_from_file_location("mammotion_cli_under_test", SCRIPT)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

DEVICE = "Luba-VS-test"


def device_state(status=13):
    return NS(report_data=NS(
        dev=NS(sys_status=status, charge_state=0, battery_val=80,
               collector_status=NS(collector_installation_status=0)),
        work=NS(area=42 << 16, progress=(12 << 16) | 30, knife_height=65),
        locations=[],
        rtk=NS(gps_stars=24, co_view_stars=20, status=4, pos_level=4, dis_status=0),
        maintenance=NS(work_time=3600, mileage=1000),
    ))


def make_cli():
    cli = app.MammotionCLI.__new__(app.MammotionCLI)
    cli.devices = []
    cli.exit_code = 0
    cli._authenticated = False
    cli._restoring_cache = None
    cli._client = Mock()
    for name in ("restore_credentials", "login_and_initiate_cloud", "stop",
                 "refresh_status", "wait_for", "send_command_with_args",
                 "send_command_and_wait", "request_report_snapshot"):
        setattr(cli._client, name, AsyncMock())
    cli._client.aliyun_device_list = []
    cli._client.mammotion_device_list = []
    cli._client.to_cache.return_value = None
    cli._client.mower.return_value = NS(
        is_transport_connected=Mock(return_value=True), iot_id="test-id",
        _reducer=NS(apply=Mock()),
    )
    cli._client.get_device_by_name.return_value = device_state()
    return cli


def report_packet(has_dev=True):
    return NS(sys=NS(toapp_report_data=NS(dev=object() if has_dev else None)))


def start_args(**overrides):
    values = dict(device=DEVICE, speed=0.25, cutting_height=2.5, path_spacing=10.0,
                  perimeter_laps=2, mowing_angle=25, pattern="chessboard",
                  mow_order="grid-first", areas=["Front"])
    values.update(overrides)
    return NS(**values)


class CLITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cli = make_cli()
        self.output = io.StringIO()
        self.redirect = redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)
        self.logs = patch.object(app.logger, "exception")
        self.logs.start()
        self.addCleanup(self.logs.stop)

    async def test_constructor_registers_async_cache_callback(self):
        client = Mock()
        with patch.object(app, "MammotionClient", return_value=client):
            cli = app.MammotionCLI()
        callback = client.on_credentials_updated
        self.assertTrue(inspect.iscoroutinefunction(callback))
        self.assertEqual(callback, cli._credentials_updated)
        with patch.object(cli, "_save_cache") as save:
            await callback()
        save.assert_called_once_with()
        self.assertEqual(cli.exit_code, 0)

    async def test_cached_login_delegates_restore_without_password_login(self):
        cache = {"token": "cached"}
        with patch.object(self.cli, "_load_cache", return_value=cache), \
                patch.object(self.cli, "_save_cache") as save:
            self.assertTrue(await self.cli.login("email", "password"))
        self.cli._client.restore_credentials.assert_awaited_once_with("email", "password", cache)
        self.cli._client.login_and_initiate_cloud.assert_not_awaited()
        save.assert_called_once()

    async def test_every_restore_exception_fails_without_fallback(self):
        for error in (RuntimeError("mqtt down"), ValueError("bad cache"), TimeoutError("timeout")):
            with self.subTest(error=type(error).__name__):
                cli = make_cli()
                cli._client.restore_credentials.side_effect = error
                with patch.object(cli, "_load_cache", return_value={"token": "cached"}), \
                        patch.object(cli, "_save_cache") as save:
                    self.assertFalse(await cli.login("email", "password"))
                cli._client.login_and_initiate_cloud.assert_not_awaited()
                save.assert_not_called()

    async def test_fresh_login_only_when_cache_missing_or_disabled(self):
        for use_cache, cache in ((True, None), (False, {"token": "cached"})):
            with self.subTest(use_cache=use_cache):
                cli = make_cli()
                with patch.object(cli, "_load_cache", return_value=cache) as load, \
                        patch.object(cli, "_save_cache"):
                    self.assertTrue(await cli.login("email", "password", use_cache=use_cache))
                cli._client.restore_credentials.assert_not_awaited()
                cli._client.login_and_initiate_cloud.assert_awaited_once_with("email", "password")
                if not use_cache:
                    load.assert_not_called()

    async def test_fresh_login_failure_returns_false(self):
        self.cli._client.login_and_initiate_cloud.side_effect = RuntimeError("denied")
        with patch.object(self.cli, "_load_cache", return_value=None):
            self.assertFalse(await self.cli.login("email", "password"))

    async def test_readiness_uses_selected_handle_and_settles(self):
        with patch.object(app.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            self.assertTrue(await self.cli._wait_for_connection(DEVICE))
        self.cli._client.mower.assert_called_with(DEVICE)
        self.cli._client._get_default_session.assert_not_called()
        sleep.assert_any_await(2.0)

    async def test_readiness_rechecks_connection_after_settling(self):
        handle = self.cli._client.mower.return_value

        async def disconnected_after_sleep(_delay):
            handle.is_transport_connected.return_value = False

        with patch.object(app.asyncio, "sleep", side_effect=disconnected_after_sleep), \
                patch.object(app.asyncio, "get_running_loop") as loop:
            loop.return_value.time.side_effect = [0.0, 0.1, 0.2, 13.0]
            self.assertFalse(await self.cli._wait_for_connection(DEVICE, timeout=12))

    async def test_readiness_timeout_even_if_unrelated_session_connected(self):
        self.cli._client.mower.return_value.is_transport_connected.return_value = False
        self.cli._client._get_default_session.return_value = NS(
            aliyun_transport=NS(is_connected=True), mammotion_transport=None)
        self.assertFalse(await self.cli._wait_for_connection(DEVICE, timeout=0))
        self.cli._client._get_default_session.assert_not_called()

    async def test_run_readiness_failure_does_not_relogin_or_delete_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "auth.json"
            cache.write_text('{"token":"preserve"}', encoding="utf-8")
            self.cli.login = AsyncMock(return_value=True)
            self.cli.stop = AsyncMock()
            self.cli._wait_for_connection = AsyncMock(return_value=False)
            handler = AsyncMock()
            args = NS(email="email", password="password", no_cache=False,
                      command="pause", device=DEVICE, func=handler)
            with patch.object(app, "AUTH_CACHE_FILE", cache):
                self.assertEqual(await self.cli.run(args), 1)
            self.assertEqual(cache.read_text(encoding="utf-8"), '{"token":"preserve"}')
        self.cli.login.assert_awaited_once_with("email", "password", use_cache=True)
        handler.assert_not_awaited()
        self.cli.stop.assert_awaited_once()
        self.cli._wait_for_connection.assert_awaited_once_with(DEVICE)

    async def test_metadata_commands_skip_readiness(self):
        for command, device in (("devices", None), ("status", "RTK-test")):
            with self.subTest(command=command):
                cli = make_cli()
                cli.login = AsyncMock(return_value=True)
                cli.stop = AsyncMock()
                cli._wait_for_connection = AsyncMock(side_effect=AssertionError("not metadata"))
                handler = AsyncMock()
                args = NS(email="email", password="password", no_cache=False,
                          command=command, device=device, func=handler)
                self.assertEqual(await cli.run(args), 0)
                cli._wait_for_connection.assert_not_awaited()
                handler.assert_awaited_once_with(args)

    async def test_run_propagates_handler_failure_exit_code(self):
        self.cli.login = AsyncMock(return_value=True)
        self.cli.stop = AsyncMock()
        self.cli._wait_for_connection = AsyncMock(return_value=True)

        async def failed(_args):
            self.cli.exit_code = 1

        args = NS(email="email", password="password", no_cache=False,
                  command="pause", device=DEVICE, func=failed)
        self.assertEqual(await self.cli.run(args), 1)

    async def test_state_accepts_fresh_unchanged_dev_report(self):
        reducer = self.cli._client.mower.return_value._reducer
        state = device_state(13)
        original = reducer.apply
        original.return_value = state

        async def refresh(name):
            self.assertEqual(name, DEVICE)
            self.assertIsNot(reducer.apply, original)
            self.assertIs(reducer.apply(state, report_packet()), state)
            # Multiple identical reports must not resolve an already-done future again.
            reducer.apply(state, report_packet())

        self.cli._client.refresh_status.side_effect = refresh
        result = await asyncio.wait_for(self.cli.get_device_state(DEVICE), timeout=1)
        self.assertEqual(result["status"], 13)
        self.assertEqual(result["progress"], 42)
        self.assertEqual(result["time_left_min"], 12)
        self.assertEqual(result["total_time_min"], 30)
        self.assertIs(reducer.apply, original)
        self.cli._client.refresh_status.assert_awaited_once_with(DEVICE)
        self.cli._client.wait_for.assert_not_awaited()
        self.cli._client.request_report_snapshot.assert_not_awaited()

    async def test_partial_reports_cannot_confirm_retained_dev_status(self):
        reducer = self.cli._client.mower.return_value._reducer
        state = device_state(13)
        original = reducer.apply
        original.return_value = state
        refreshed = asyncio.Event()

        async def refresh(_name):
            # Work/RTK-only packets retain the previous nonzero dev model.
            reducer.apply(state, report_packet(has_dev=False))
            reducer.apply(state, report_packet(has_dev=False))
            refreshed.set()

        self.cli._client.refresh_status.side_effect = refresh
        task = asyncio.create_task(self.cli._wait_for_fresh_status(
            DEVICE, lambda updated: updated.report_data.dev.sys_status != 0, timeout=1))
        await asyncio.wait_for(refreshed.wait(), timeout=1)
        self.assertFalse(task.done(), "partial reports must not resolve the waiter")
        reducer.apply(state, report_packet())
        self.assertIs(await asyncio.wait_for(task, timeout=1), state)
        self.assertIs(reducer.apply, original)

    async def test_state_rejects_fresh_zero_status_until_nonzero_dev_report(self):
        reducer = self.cli._client.mower.return_value._reducer
        original = reducer.apply
        state = device_state(0)
        original.return_value = state
        refreshed = asyncio.Event()

        async def refresh(_name):
            reducer.apply(state, report_packet())
            refreshed.set()

        self.cli._client.refresh_status.side_effect = refresh
        task = asyncio.create_task(self.cli.get_device_state(DEVICE))
        await asyncio.wait_for(refreshed.wait(), timeout=1)
        self.assertFalse(task.done())
        state.report_data.dev.sys_status = 13
        reducer.apply(state, report_packet())
        self.assertEqual((await asyncio.wait_for(task, timeout=1))["status"], 13)
        self.assertIs(reducer.apply, original)

    async def test_state_timeout_does_not_return_cached_state(self):
        self.cli._wait_for_fresh_status = AsyncMock(side_effect=TimeoutError("no fresh state"))
        self.assertIsNone(await self.cli.get_device_state(DEVICE))
        call = self.cli._wait_for_fresh_status.await_args
        predicate = call.args[1]
        self.assertFalse(predicate(device_state(0)))
        self.assertTrue(predicate(device_state(13)))

    async def test_fresh_status_timeout_restores_reducer(self):
        reducer = self.cli._client.mower.return_value._reducer
        original = reducer.apply
        original.return_value = device_state(13)
        self.cli._client.refresh_status.side_effect = lambda _name: reducer.apply(
            device_state(13), report_packet(has_dev=False))
        with self.assertRaises(TimeoutError):
            await self.cli._wait_for_fresh_status(DEVICE, lambda state: True, timeout=0.01)
        self.assertIs(reducer.apply, original)

    async def test_fresh_status_refresh_exception_restores_reducer(self):
        reducer = self.cli._client.mower.return_value._reducer
        original = reducer.apply

        async def refresh(_name):
            self.assertIsNot(reducer.apply, original)
            raise RuntimeError("refresh failed")

        self.cli._client.refresh_status.side_effect = refresh
        with self.assertRaisesRegex(RuntimeError, "refresh failed"):
            await self.cli._wait_for_fresh_status(DEVICE, lambda state: True)
        self.assertIs(reducer.apply, original)

    async def test_fresh_status_predicate_exception_restores_reducer(self):
        reducer = self.cli._client.mower.return_value._reducer
        original = reducer.apply
        original.return_value = device_state(13)

        async def refresh(_name):
            reducer.apply(device_state(13), report_packet())

        def invalid_predicate(_state):
            raise ValueError("predicate failed")

        self.cli._client.refresh_status.side_effect = refresh
        with self.assertRaisesRegex(ValueError, "predicate failed"):
            await self.cli._wait_for_fresh_status(DEVICE, invalid_predicate)
        self.assertIs(reducer.apply, original)

    async def test_fresh_status_cancellation_restores_reducer(self):
        reducer = self.cli._client.mower.return_value._reducer
        original = reducer.apply
        refreshed = asyncio.Event()

        async def refresh(_name):
            refreshed.set()

        self.cli._client.refresh_status.side_effect = refresh
        task = asyncio.create_task(self.cli._wait_for_fresh_status(DEVICE, lambda state: True))
        await asyncio.wait_for(refreshed.wait(), timeout=1)
        self.assertIsNot(reducer.apply, original)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIs(reducer.apply, original)

    async def test_action_priority_rejects_pre_delivery_partial_and_wrong_status_reports(self):
        reducer = self.cli._client.mower.return_value._reducer
        original = reducer.apply
        state = device_state(19)
        original.return_value = state
        refreshed = asyncio.Event()

        async def during_delivery(*args, **kwargs):
            self.assertIs(reducer.apply, original, "confirmation must begin after delivery")
            reducer.apply(state, report_packet())

        async def refresh(_name):
            reducer.apply(state, report_packet(has_dev=False))
            state.report_data.dev.sys_status = 13
            reducer.apply(state, report_packet())
            refreshed.set()

        self.cli._client.send_command_with_args.side_effect = during_delivery
        self.cli._client.refresh_status.side_effect = refresh
        task = asyncio.create_task(self.cli._send_action(DEVICE, "pause_execute_task", {19}))
        await asyncio.wait_for(refreshed.wait(), timeout=1)
        self.assertFalse(task.done(), "pre-delivery, partial and wrong-status reports cannot confirm")
        state.report_data.dev.sys_status = 19
        reducer.apply(state, report_packet())
        await asyncio.wait_for(task, timeout=1)
        sent = self.cli._client.send_command_with_args.await_args
        self.assertEqual(sent.args, (DEVICE, "pause_execute_task"))
        self.assertEqual(sent.kwargs["priority"], app.Priority.USER)
        self.assertIs(reducer.apply, original)
        self.cli._client.wait_for.assert_not_awaited()

    async def test_action_timeout_is_not_silently_successful(self):
        self.cli._wait_for_fresh_status = AsyncMock(side_effect=TimeoutError("no confirmation"))
        with self.assertRaises((TimeoutError, RuntimeError)):
            await self.cli._send_action(DEVICE, "pause_execute_task", {19})
        self.assertEqual(self.cli._wait_for_fresh_status.await_args.kwargs["timeout"], 30.0)

    async def test_return_ready_confirmation_requires_docked_state(self):
        for final_status, charge in ((11, 1), (14, 0)):
            with self.subTest(final_status=final_status):
                cli = make_cli()
                reducer = cli._client.mower.return_value._reducer
                original = reducer.apply
                state = device_state(11)
                original.return_value = state
                refreshed = asyncio.Event()

                async def refresh(_name):
                    reducer.apply(state, report_packet())
                    refreshed.set()

                cli._client.refresh_status.side_effect = refresh
                task = asyncio.create_task(cli._send_action(DEVICE, "return_to_dock", {11, 14, 15}))
                await asyncio.wait_for(refreshed.wait(), timeout=1)
                self.assertFalse(task.done(), "READY without charging is not dock confirmation")
                state.report_data.dev.sys_status = final_status
                state.report_data.dev.charge_state = charge
                reducer.apply(state, report_packet())
                await asyncio.wait_for(task, timeout=1)
                self.assertIs(reducer.apply, original)

    async def test_action_delivery_failure_does_not_wait_or_retry(self):
        self.cli._client.send_command_with_args.side_effect = RuntimeError("delivery failed")
        self.cli._wait_for_fresh_status = AsyncMock()
        with self.assertRaises(RuntimeError):
            await self.cli._send_action(DEVICE, "pause_execute_task", {19})
        self.cli._client.send_command_with_args.assert_awaited_once()
        self.cli._wait_for_fresh_status.assert_not_awaited()
        self.cli._client.refresh_status.assert_not_awaited()

    async def test_action_callers_fail_nonzero_and_do_not_print_success(self):
        cases = (("cmd_pause", 13, "paused "), ("cmd_resume", 19, "resumed mowing"),
                 ("cmd_return", 11, "returning to dock"), ("cmd_cancel", 13, "cancelled task"))
        for method, status, success in cases:
            with self.subTest(method=method):
                cli = make_cli()
                cli.get_device_state = AsyncMock(return_value={"status": status, "status_name": "test"})
                cli._send_action = AsyncMock(side_effect=TimeoutError("no confirmation"))
                output = io.StringIO()
                with redirect_stdout(output):
                    await getattr(cli, method)(NS(device=DEVICE))
                self.assertEqual(cli.exit_code, 1)
                self.assertNotIn(success, output.getvalue())
                cli._send_action.assert_awaited_once()

    async def test_action_handlers_use_correct_commands_and_confirmations(self):
        cases = (("cmd_pause", 13, "pause_execute_task", {19, 39}),
                 ("cmd_resume", 19, "resume_execute_task", {13}),
                 ("cmd_return", 11, "return_to_dock", {11, 14, 15}),
                 ("cmd_cancel", 13, "cancel_job", {1, 11, 15}))
        for method, status, command, expected in cases:
            with self.subTest(method=method):
                cli = make_cli()
                cli.get_device_state = AsyncMock(return_value={"status": status, "status_name": "test"})
                cli._send_action = AsyncMock()
                await getattr(cli, method)(NS(device=DEVICE))
                cli._send_action.assert_awaited_once_with(DEVICE, command, expected)
                self.assertEqual(cli.exit_code, 0)

    async def test_blocked_preconditions_do_not_send_actions(self):
        for method in ("cmd_pause", "cmd_resume", "cmd_return", "cmd_cancel"):
            with self.subTest(method=method):
                cli = make_cli()
                cli.get_device_state = AsyncMock(return_value={"status": 2, "status_name": "offline"})
                cli._send_action = AsyncMock()
                await getattr(cli, method)(NS(device=DEVICE))
                self.assertEqual(cli.exit_code, 1)
                cli._send_action.assert_not_awaited()
                cli._client.send_command_with_args.assert_not_awaited()

    async def test_missing_state_is_nonzero(self):
        self.cli.get_device_state = AsyncMock(return_value=None)
        self.cli._send_action = AsyncMock()
        await self.cli.cmd_pause(NS(device=DEVICE))
        self.assertEqual(self.cli.exit_code, 1)
        self.cli._send_action.assert_not_awaited()

    async def test_start_rejects_all_nonfinite_numeric_inputs(self):
        for field in ("speed", "cutting_height", "path_spacing"):
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(field=field, value=value):
                    cli = make_cli()
                    cli.get_area_list = AsyncMock(return_value=[NS(name="Front", hash=123)])
                    cli._send_action = AsyncMock()
                    await cli.cmd_start(start_args(**{field: value}))
                    self.assertEqual(cli.exit_code, 1)
                    cli.get_area_list.assert_not_awaited()
                    cli._client.send_command_and_wait.assert_not_awaited()
                    cli._send_action.assert_not_awaited()

    async def test_start_rejects_unsupported_luba1_and_yuka(self):
        for name in ("Luba-test", "Yuka-test", "Yuka-MN-test"):
            with self.subTest(device=name):
                cli = make_cli()
                cli.get_area_list = AsyncMock()
                cli._send_action = AsyncMock()
                output = io.StringIO()
                with redirect_stdout(output):
                    await cli.cmd_start(start_args(device=name))
                self.assertEqual(cli.exit_code, 1)
                self.assertRegex(output.getvalue().lower(), r"unsupported|supports .* only")
                cli.get_area_list.assert_not_awaited()
                cli._client.send_command_and_wait.assert_not_awaited()
                cli._send_action.assert_not_awaited()

    async def test_start_builds_chessboard_geometry_and_confirms_working(self):
        self.cli.get_device_state = AsyncMock(return_value={"status": 11, "status_name": "ready"})
        self.cli.get_area_list = AsyncMock(return_value=[NS(name="Front", hash=123)])
        self.cli._send_action = AsyncMock()
        await self.cli.cmd_start(start_args())
        self.assertEqual(self.cli.exit_code, 0)
        route_call = self.cli._client.send_command_and_wait.await_args
        self.assertIsNotNone(route_call)
        route = route_call.kwargs["generate_route_information"]
        self.assertEqual(route_call.args, (DEVICE, "generate_route_information", "bidire_reqconver_path"))
        self.assertEqual(route_call.kwargs["priority"], app.Priority.USER)
        self.assertEqual(route.one_hashs, [123])
        self.assertEqual(route.channel_mode, 1)
        self.assertEqual(route.toward_included_angle, 90)
        self.assertEqual(route.toward, 25)
        self.assertEqual(route.channel_width, 25)
        self.assertEqual(route.blade_height, 65)
        self.assertEqual(route.speed, 0.25)
        self.assertEqual(route.path_order.encode("latin-1")[5], 8)
        action = self.cli._send_action.await_args
        self.assertEqual(action.args[:2], (DEVICE, "start_job"))
        statuses = action.args[2] if len(action.args) > 2 else action.kwargs["expected_statuses"]
        self.assertIn(app.MammotionWorkMode.WORKING.value, statuses)

    async def test_start_planning_failure_prevents_start(self):
        self.cli.get_device_state = AsyncMock(return_value={"status": 11, "status_name": "ready"})
        self.cli.get_area_list = AsyncMock(return_value=[NS(name="Front", hash=123)])
        self.cli._client.send_command_and_wait.side_effect = TimeoutError("route rejected")
        self.cli._send_action = AsyncMock()
        await self.cli.cmd_start(start_args())
        self.assertEqual(self.cli.exit_code, 1)
        self.cli._send_action.assert_not_awaited()
        self.assertNotIn("started mowing", self.output.getvalue())

    async def test_start_confirmation_failure_is_nonzero(self):
        self.cli.get_device_state = AsyncMock(return_value={"status": 11, "status_name": "ready"})
        self.cli.get_area_list = AsyncMock(return_value=[NS(name="Front", hash=123)])
        self.cli._send_action = AsyncMock(side_effect=TimeoutError("no WORKING report"))
        await self.cli.cmd_start(start_args())
        self.assertEqual(self.cli.exit_code, 1)
        self.assertNotIn("started mowing", self.output.getvalue())

    async def test_luba1_areas_explicitly_unsupported(self):
        await self.cli.cmd_areas(NS(device="Luba-test"))
        self.assertEqual(self.cli.exit_code, 1)
        self.assertRegex(self.output.getvalue().lower(), r"unsupported|not supported")
        self.cli._client.send_command_and_wait.assert_not_awaited()
        self.cli._client.send_command_with_args.assert_not_awaited()

    async def test_schedule_reads_fresh_replies_at_user_priority(self):
        self.cli.get_area_list = AsyncMock(return_value=[])
        wire1, wire2 = object(), object()
        self.cli._client.send_command_and_wait.side_effect = [
            NS(nav=NS(todev_planjob_set=wire1)), NS(nav=NS(todev_planjob_set=wire2))]
        plans = [app.Plan(plan_id="one", total_plan_num=2, task_name="Fresh one"),
                 app.Plan(plan_id="two", total_plan_num=2, task_name="Fresh two")]
        with patch.object(app.Plan, "from_wire", side_effect=plans) as decode:
            await self.cli.cmd_schedule(NS(device=DEVICE, verbose=False))
        self.assertEqual(self.cli.exit_code, 0)
        self.assertEqual(decode.call_args_list[0].args, (wire1, DEVICE))
        self.assertEqual(decode.call_args_list[1].args, (wire2, DEVICE))
        calls = self.cli._client.send_command_and_wait.await_args_list
        self.assertEqual([call.kwargs["plan_index"] for call in calls], [0, 1])
        for call in calls:
            self.assertEqual(call.args, (DEVICE, "read_plan", "todev_planjob_set"))
            self.assertEqual(call.kwargs["priority"], app.Priority.USER)
        self.cli._client.get_device_by_name.assert_not_called()
        self.assertIn("Fresh one", self.output.getvalue())
        self.assertIn("Fresh two", self.output.getvalue())

    async def test_schedule_missing_or_repeated_ids_fail_nonzero(self):
        for ids in (("",), ("same", "same")):
            with self.subTest(ids=ids):
                cli = make_cli()
                cli.get_area_list = AsyncMock(return_value=[])
                cli._client.send_command_and_wait.return_value = NS(nav=NS(todev_planjob_set=object()))
                plans = [app.Plan(plan_id=plan_id, total_plan_num=2) for plan_id in ids]
                with patch.object(app.Plan, "from_wire", side_effect=plans):
                    await cli.cmd_schedule(NS(device=DEVICE, verbose=False))
                self.assertEqual(cli.exit_code, 1)
                self.assertEqual(cli._client.send_command_and_wait.await_count, len(ids))
                cli._client.get_device_by_name.assert_not_called()

    async def test_schedule_timeout_cannot_print_cached_success(self):
        self.cli.get_area_list = AsyncMock(return_value=[])
        self.cli._client.send_command_and_wait.side_effect = TimeoutError("no fresh plans")
        self.cli._client.get_device_by_name.return_value = NS(map=NS(plan={"cached": Mock()}))
        await self.cli.cmd_schedule(NS(device=DEVICE, verbose=False))
        self.assertEqual(self.cli.exit_code, 1)
        self.assertNotIn("Schedules for", self.output.getvalue())
        self.cli._client.get_device_by_name.assert_not_called()

    async def test_reports_restore_reducer_on_exception_and_cancellation(self):
        for error in (RuntimeError("send failed"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                cli = make_cli()
                reducer = cli._client.mower.return_value._reducer
                original = reducer.apply

                async def fail(*args, **kwargs):
                    self.assertIsNot(reducer.apply, original, "report interceptor must be installed")
                    raise error

                cli._client.send_command_with_args.side_effect = fail
                args = NS(device=DEVICE, count=2, content_only=False, today_only=False, verbose=False)
                if isinstance(error, asyncio.CancelledError):
                    with self.assertRaises(asyncio.CancelledError):
                        await cli.cmd_reports(args)
                else:
                    await cli.cmd_reports(args)
                    self.assertEqual(cli.exit_code, 1)
                self.assertIs(reducer.apply, original)

    async def test_rotated_tokens_callback_preserves_other_cache_blocks_on_failed_restore(self):
        previous = {
            "mammotion_data": {"token": "old"},
            "aep": {"token": "aliyun"},
            "devices": [{"device_name": DEVICE}],
        }
        rotated = {"mammotion_data": {"token": "rotated"}}
        self.cli._client.to_cache.return_value = rotated

        async def restore(_email, _password, cache):
            self.assertEqual(cache, previous)
            await self.cli._credentials_updated()
            raise ConnectionError("transport restore failed after token rotation")

        self.cli._client.restore_credentials.side_effect = restore
        with tempfile.TemporaryDirectory() as tmp:
            cache_file = Path(tmp) / "auth.json"
            cache_file.write_bytes(app.orjson.dumps(previous))
            with patch.object(app, "AUTH_CACHE_FILE", cache_file):
                self.assertFalse(await self.cli.login("email", "password"))
                saved = app.orjson.loads(cache_file.read_bytes())
                self.assertEqual(saved, {**previous, **rotated})
                self.assertFalse(self.cli._authenticated)
                self.assertEqual(self.cli._restoring_cache, previous)
                # Shutdown must not replace the merged cache with the partial client cache.
                await self.cli.stop()
                self.assertEqual(app.orjson.loads(cache_file.read_bytes()), saved)
        self.assertEqual(previous["mammotion_data"], {"token": "old"})
        self.cli._client.login_and_initiate_cloud.assert_not_awaited()

    async def test_failed_restore_shutdown_preserves_existing_cache(self):
        self.cli._client.restore_credentials.side_effect = ConnectionError("offline")
        self.cli._client.to_cache.return_value = {"partial": "must not replace cache"}
        with patch.object(self.cli, "_load_cache", return_value={"token": "good"}), \
                patch.object(self.cli, "_save_cache") as save:
            self.assertFalse(await self.cli.login("email", "password"))
            await self.cli.stop()
        save.assert_not_called()
        self.cli._client.stop.assert_awaited_once()

    async def test_area_list_decodes_only_fresh_response(self):
        self.cli._client.send_command_and_wait.return_value = NS(
            nav=NS(toapp_all_hash_name=NS(hashnames=[NS(name="Front Yard", hash=123)])))
        areas = await self.cli.get_area_list(DEVICE)
        self.assertEqual([(area.name, area.hash) for area in areas], [("Front Yard", 123)])
        self.cli._client.get_device_by_name.assert_not_called()
        self.assertEqual(self.cli._client.send_command_and_wait.await_args.kwargs["priority"], app.Priority.USER)

    async def test_area_timeout_does_not_return_cached_names(self):
        self.cli._client.send_command_and_wait.side_effect = TimeoutError("no areas reply")
        self.assertEqual(await self.cli.get_area_list(DEVICE), [])
        self.assertEqual(self.cli.exit_code, 1)
        self.cli._client.get_device_by_name.assert_not_called()

    async def test_stop_saves_cache_before_client_shutdown(self):
        self.cli._authenticated = True
        events = []
        self.cli._save_cache = Mock(side_effect=lambda: events.append("save"))

        async def stop():
            events.append("stop")

        self.cli._client.stop.side_effect = stop
        await self.cli.stop()
        self.assertEqual(events, ["save", "stop"])


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.cli = make_cli()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "auth.json"
        self.patch = patch.object(app, "AUTH_CACHE_FILE", self.cache)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_load_only_accepts_nonempty_dict(self):
        for content in ('[]', '[1]', '"token"', 'true', '42', 'null', '{}', 'broken'):
            with self.subTest(content=content):
                self.cache.write_text(content, encoding="utf-8")
                self.assertIsNone(self.cli._load_cache())
        self.cache.write_text('{"token":"valid"}', encoding="utf-8")
        self.assertEqual(self.cli._load_cache(), {"token": "valid"})

    def test_missing_cache_returns_none(self):
        self.assertIsNone(self.cli._load_cache())

    def test_cache_write_replaces_atomically_in_same_directory(self):
        self.cache.write_text('{"old":true}', encoding="utf-8")
        self.cli._client.to_cache.return_value = {"token": "new"}
        original_replace = os.replace
        replacements = []

        def replace(source, target):
            source, target = Path(source), Path(target)
            self.assertNotEqual(source, self.cache)
            self.assertEqual(source.parent, self.cache.parent)
            self.assertEqual(target, self.cache)
            self.assertEqual(app.orjson.loads(source.read_bytes()), {"token": "new"})
            self.assertEqual(app.orjson.loads(self.cache.read_bytes()), {"old": True})
            replacements.append((source, target))
            original_replace(source, target)

        with patch.object(app.os, "replace", side_effect=replace):
            self.cli._save_cache()
        self.assertEqual(len(replacements), 1)
        self.assertEqual(self.cli._load_cache(), {"token": "new"})
        self.assertEqual(list(self.cache.parent.iterdir()), [self.cache])

    def test_failed_replace_preserves_old_cache_and_cleans_tempfile(self):
        self.cache.write_text('{"old":true}', encoding="utf-8")
        self.cli._client.to_cache.return_value = {"token": "new"}
        with patch.object(app.os, "replace", side_effect=OSError("disk failure")):
            self.cli._save_cache()
        self.assertEqual(self.cli._load_cache(), {"old": True})
        self.assertEqual(list(self.cache.parent.iterdir()), [self.cache])


if __name__ == "__main__":
    unittest.main()
