import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from src.domain.value_objects.report_delivery import GroupReportTarget
from src.infrastructure.reporting.dispatcher import ReportDispatcher
from src.infrastructure.scheduler.auto_scheduler import AutoScheduler


def test_dispatch_returns_false_when_no_report_format_is_sent():
    async def scenario():
        config_manager = SimpleNamespace(
            get_output_format=Mock(return_value=["text"]),
            get_report_delivery_mode=Mock(return_value="source_group"),
        )
        dispatcher = ReportDispatcher(config_manager, None, None)
        dispatcher._dispatch_text = AsyncMock(return_value=False)

        assert await dispatcher.dispatch("123456", {}, "onebot-main") is False

        dispatcher._dispatch_text.return_value = True
        assert await dispatcher.dispatch("123456", {}, "onebot-main") is True

    asyncio.run(scenario())


def test_forward_delivery_uses_target_group_without_source_fallback():
    async def scenario():
        source_adapter = SimpleNamespace(
            get_platform_name=Mock(return_value="onebot"),
            get_group_info=AsyncMock(return_value=None),
            send_text_report=AsyncMock(return_value=True),
        )
        target_adapter = SimpleNamespace(
            get_platform_name=Mock(return_value="onebot"),
            send_text_report=AsyncMock(return_value=True),
        )
        bot_manager = SimpleNamespace(
            get_adapter=Mock(
                side_effect=lambda platform_id: {
                    "source-platform": source_adapter,
                    "target-platform": target_adapter,
                }.get(platform_id)
            )
        )
        message_sender = SimpleNamespace(
            bot_manager=bot_manager,
            send_text=AsyncMock(return_value=False),
        )
        config_manager = SimpleNamespace(
            get_report_delivery_mode=Mock(return_value="forward_group"),
            get_forward_report_target=Mock(
                return_value=GroupReportTarget("target-group", "target-platform")
            ),
            get_output_format=Mock(return_value=["text"]),
        )
        report_generator = SimpleNamespace(
            generate_text_report=Mock(return_value="源群总结正文")
        )
        dispatcher = ReportDispatcher(config_manager, report_generator, message_sender)

        assert await dispatcher.dispatch("source-group", {}, "source-platform") is True

        target_adapter.send_text_report.assert_awaited_once_with(
            "target-group", "来源群：source-group\n\n源群总结正文"
        )
        source_adapter.send_text_report.assert_not_awaited()
        message_sender.send_text.assert_not_awaited()

    asyncio.run(scenario())


def test_serverchan_delivery_does_not_require_platform_adapter():
    async def scenario():
        config_manager = SimpleNamespace(
            get_report_delivery_mode=Mock(return_value="serverchan"),
            get_serverchan_bot_config=Mock(
                return_value=SimpleNamespace(token="token", chat_id=123, silent=False)
            ),
        )
        report_generator = SimpleNamespace(
            generate_text_report=Mock(return_value="源群总结正文")
        )
        message_sender = SimpleNamespace(bot_manager=SimpleNamespace())
        dispatcher = ReportDispatcher(config_manager, report_generator, message_sender)
        dispatcher.serverchan_sender.send_summary = AsyncMock(return_value=True)

        assert await dispatcher.dispatch("source-group", {}, None) is True
        dispatcher.serverchan_sender.send_summary.assert_awaited_once()
        assert dispatcher.serverchan_sender.send_summary.await_args.args[1].endswith(
            "源群总结正文"
        )

    asyncio.run(scenario())


def test_feishu_delivery_does_not_require_platform_adapter():
    async def scenario():
        config_manager = SimpleNamespace(
            get_report_delivery_mode=Mock(return_value="feishu"),
            get_feishu_bot_config=Mock(
                return_value=SimpleNamespace(
                    app_id="cli_xxx",
                    app_secret="secret",
                    user_id="ou_abc",
                    user_id_type="open_id",
                )
            ),
        )
        report_generator = SimpleNamespace(
            generate_text_report=Mock(return_value="源群总结正文")
        )
        message_sender = SimpleNamespace(bot_manager=SimpleNamespace())
        dispatcher = ReportDispatcher(config_manager, report_generator, message_sender)
        dispatcher.feishu_sender.send_summary = AsyncMock(return_value=True)

        assert await dispatcher.dispatch("source-group", {}, None) is True
        dispatcher.feishu_sender.send_summary.assert_awaited_once()
        assert dispatcher.feishu_sender.send_summary.await_args.args[1].endswith(
            "源群总结正文"
        )

    asyncio.run(scenario())


def test_traditional_analysis_is_failed_when_report_delivery_fails():
    async def scenario():
        mock_comic_handler = SimpleNamespace(try_trigger_comic_generation=Mock())

        async def failing_dispatch(*_args):
            assert mock_comic_handler.try_trigger_comic_generation.called
            return False

        scheduler = object.__new__(AutoScheduler)
        scheduler._terminating = False
        scheduler._get_group_name_safe = AsyncMock(return_value="测试群")
        scheduler.bot_manager = SimpleNamespace(
            is_ready_for_auto_analysis=Mock(return_value=True)
        )
        scheduler.analysis_service = SimpleNamespace(
            execute_daily_analysis=AsyncMock(
                return_value={
                    "success": True,
                    "analysis_result": {},
                    "adapter": SimpleNamespace(platform_id="onebot-main"),
                }
            )
        )
        scheduler.report_dispatcher = SimpleNamespace(
            dispatch=AsyncMock(side_effect=failing_dispatch)
        )
        scheduler.config_manager = SimpleNamespace(
            get_analysis_days=Mock(return_value=1),
            get_report_delivery_mode=Mock(return_value="source_group"),
        )
        scheduler.plugin_instance = SimpleNamespace(
            comic_command_handler=mock_comic_handler
        )

        result = await scheduler._perform_auto_analysis_for_group(
            "123456", "onebot-main"
        )

        assert result["analysis_success"] is True
        assert result["report_sent"] is False
        assert result["success"] is False
        assert result["reason"] == "report_delivery_failed"
        mock_comic_handler.try_trigger_comic_generation.assert_called_once_with(
            "123456", "onebot-main", {}
        )

        scheduler.analysis_service.execute_daily_analysis.return_value = {
            "success": True,
            "analysis_result": {},
            "adapter": SimpleNamespace(platform_id="onebot-main"),
        }
        scheduler.report_dispatcher.dispatch.side_effect = None
        scheduler.report_dispatcher.dispatch.return_value = True
        result = await scheduler._perform_auto_analysis_for_group(
            "123456", "onebot-main"
        )

        assert result["analysis_success"] is True
        assert result["report_sent"] is True
        assert result["success"] is True
        assert mock_comic_handler.try_trigger_comic_generation.call_count == 2
        mock_comic_handler.try_trigger_comic_generation.assert_called_with(
            "123456", "onebot-main", {}
        )

    asyncio.run(scenario())


def test_incremental_final_report_requires_successful_delivery():
    async def scenario():
        mock_comic_handler = SimpleNamespace(try_trigger_comic_generation=Mock())

        async def failing_dispatch(*_args):
            assert mock_comic_handler.try_trigger_comic_generation.called
            return False

        scheduler = object.__new__(AutoScheduler)
        scheduler._terminating = False
        scheduler._get_group_name_safe = AsyncMock(return_value="测试群")
        scheduler.bot_manager = SimpleNamespace(
            is_ready_for_auto_analysis=Mock(return_value=True)
        )
        scheduler.analysis_service = SimpleNamespace(
            execute_incremental_final_report=AsyncMock(
                return_value={
                    "success": True,
                    "analysis_result": {},
                    "adapter": SimpleNamespace(platform_id="onebot-main"),
                }
            ),
            incremental_store=None,
        )
        scheduler.report_dispatcher = SimpleNamespace(
            dispatch=AsyncMock(side_effect=failing_dispatch)
        )
        scheduler.config_manager = SimpleNamespace(
            get_analysis_days=Mock(return_value=1),
            get_report_delivery_mode=Mock(return_value="source_group"),
        )
        scheduler.plugin_instance = SimpleNamespace(
            comic_command_handler=mock_comic_handler
        )

        result = await scheduler._perform_incremental_final_report_for_group(
            "123456", "onebot-main"
        )

        assert result["analysis_success"] is True
        assert result["report_sent"] is False
        assert result["success"] is False
        assert result["reason"] == "report_delivery_failed"
        mock_comic_handler.try_trigger_comic_generation.assert_called_once_with(
            "123456", "onebot-main", {}
        )

        scheduler.analysis_service.execute_incremental_final_report.return_value = {
            "success": True,
            "analysis_result": {},
            "adapter": SimpleNamespace(platform_id="onebot-main"),
        }
        scheduler.report_dispatcher.dispatch.side_effect = None
        scheduler.report_dispatcher.dispatch.return_value = True
        result = await scheduler._perform_incremental_final_report_for_group(
            "123456", "onebot-main"
        )

        assert result["analysis_success"] is True
        assert result["report_sent"] is True
        assert result["success"] is True
        assert mock_comic_handler.try_trigger_comic_generation.call_count == 2
        mock_comic_handler.try_trigger_comic_generation.assert_called_with(
            "123456", "onebot-main", {}
        )

    asyncio.run(scenario())


def test_scheduled_traditional_reports_observe_and_release_dispatch_slot():
    """定时普通全量分析应通过调度槽位串行控制并在结束后释放。"""
    active = 0
    peak_active = 0
    calls = []

    async def run_group(group_id, platform_id):
        nonlocal active, peak_active
        active += 1
        peak_active = max(peak_active, active)
        calls.append((group_id, platform_id))
        await asyncio.sleep(0)
        active -= 1
        return {"success": True}

    async def scenario():
        scheduler = object.__new__(AutoScheduler)
        scheduler._terminating = False
        scheduler.incremental_trigger = None
        scheduler.config_manager = SimpleNamespace(
            get_max_concurrent_tasks=Mock(return_value=1),
            get_stagger_seconds=Mock(return_value=0),
        )
        scheduler._get_scheduled_targets = AsyncMock(
            return_value=[
                ("group-a", "onebot-main", "traditional"),
                ("group-b", "onebot-main", "traditional"),
            ]
        )
        scheduler._perform_auto_analysis_for_group_with_timeout = run_group

        await scheduler._run_scheduled_report()

        assert calls == [
            ("group-a", "onebot-main"),
            ("group-b", "onebot-main"),
        ]
        assert peak_active == 1

    asyncio.run(scenario())


def test_incremental_final_report_does_not_send_after_target_is_removed():
    async def scenario():
        scheduler = object.__new__(AutoScheduler)
        scheduler._terminating = False
        scheduler._get_group_name_safe = AsyncMock(return_value="测试群")
        scheduler.bot_manager = SimpleNamespace(
            is_ready_for_auto_analysis=Mock(return_value=True)
        )
        scheduler.analysis_service = SimpleNamespace(
            execute_incremental_final_report=AsyncMock(
                return_value={
                    "success": True,
                    "analysis_result": {},
                    "adapter": SimpleNamespace(platform_id="onebot-main"),
                }
            ),
            incremental_store=None,
        )
        scheduler.report_dispatcher = SimpleNamespace(dispatch=AsyncMock())
        scheduler.config_manager = SimpleNamespace(
            get_analysis_days=Mock(return_value=1)
        )
        scheduler.incremental_trigger = SimpleNamespace(
            is_target_group=Mock(side_effect=[True, False])
        )

        result = await scheduler._perform_incremental_final_report_for_group(
            "123456", "onebot-main"
        )

        scheduler.analysis_service.execute_incremental_final_report.assert_awaited_once()
        scheduler.report_dispatcher.dispatch.assert_not_awaited()
        assert result["success"] is False
        assert result["analysis_success"] is True
        assert result["report_sent"] is False
        assert result["reason"] == "target_removed"

    asyncio.run(scenario())


def test_immediate_incremental_reports_are_coalesced_per_group():
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def report(group_id, platform_id):
        calls.append((group_id, platform_id))
        started.set()
        await release.wait()
        return {"success": True}

    async def scenario():
        scheduler = object.__new__(AutoScheduler)
        scheduler._terminating = False
        scheduler.config_manager = SimpleNamespace(
            get_incremental_report_immediately=Mock(return_value=True)
        )
        scheduler._immediate_report_tasks = {}
        scheduler._immediate_report_versions = {}
        scheduler._perform_incremental_final_report_for_group_with_timeout = report

        scheduler._request_immediate_incremental_report("123456", "onebot-main")
        await started.wait()
        scheduler._request_immediate_incremental_report("123456", "onebot-main")
        scheduler._request_immediate_incremental_report("123456", "onebot-main")

        release.set()
        for _ in range(100):
            if not scheduler._immediate_report_tasks:
                break
            await asyncio.sleep(0)

        assert calls == [
            ("123456", "onebot-main"),
            ("123456", "onebot-main"),
        ]
        assert scheduler._immediate_report_versions == {}

    asyncio.run(scenario())


def test_fallback_does_not_mask_failed_report_delivery():
    async def scenario():
        scheduler = object.__new__(AutoScheduler)
        scheduler._perform_auto_analysis_for_group_with_timeout = AsyncMock(
            return_value={
                "success": False,
                "analysis_success": True,
                "report_sent": False,
                "reason": "report_delivery_failed",
            }
        )

        result = await scheduler._fallback_to_traditional("123456", "onebot-main")

        assert result["success"] is False
        assert result["fallback"] is True
        assert result["report_sent"] is False

    asyncio.run(scenario())
