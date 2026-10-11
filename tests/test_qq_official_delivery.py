"""QQ 官方独立投递、停止原群和组合通道的行为回归。"""

import asyncio
from itertools import product
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.application.handlers.analysis_command_handler import AnalysisCommandHandler
from src.domain.value_objects.report_delivery import (
    GroupReportTarget,
    QQOfficialPrivateTarget,
)
from src.infrastructure.config.config_manager import ConfigManager
from src.infrastructure.config.config_migrator import ConfigMigrator
from src.infrastructure.platform.bot_manager import BotManager
from src.infrastructure.reporting.dispatcher import ReportDispatcher
from src.infrastructure.scheduler.auto_scheduler import AutoScheduler
from src.shared.trace_context import TraceContext, _current_trace


@pytest.fixture
def delivery_rig(tmp_path):
    """使用真实配置、实例选择及分发器，隔离渲染和外部请求。"""
    settings = {
        "destination": "independent",
        "stop_source_group": False,
        "forward_group_enabled": False,
        "qq_official_enabled": False,
        "serverchan_enabled": False,
        "forward_target": "target-platform:GroupMessage:target-group",
        "qq_official_target": "official-main:GroupMessage:official-group",
        "serverchan_bot_token": "secret-token",
        "serverchan_chat_id": "123",
    }
    config = object.__new__(ConfigManager)
    config.config = {
        "report_delivery": settings,
        "basic": {"output_format": ["text"], "show_report_caption": False},
    }

    def adapter(platform_id, name):
        return SimpleNamespace(
            platform_id=platform_id,
            get_platform_name=Mock(return_value=name),
            send_text_report=AsyncMock(return_value=True),
            send_private_text_report=AsyncMock(return_value=True),
            send_private_text=AsyncMock(return_value=True),
            send_private_image=AsyncMock(return_value=True),
            send_private_file=AsyncMock(return_value=True),
        )

    source = adapter("source-platform", "onebot")
    target = adapter("target-platform", "onebot")
    official = adapter("official-main", "qq_official")
    bot_manager = BotManager(config)
    bot_manager._adapters = {
        "source-platform": source,
        "target-platform": target,
        "official-main": official,
    }
    sender = SimpleNamespace(
        bot_manager=bot_manager,
        send_text=AsyncMock(return_value=False),
        send_image_smart=AsyncMock(return_value=False),
        send_file=AsyncMock(return_value=False),
    )
    generator = SimpleNamespace(
        data_dir=tmp_path,
        generate_text_report=Mock(return_value="源群总结正文"),
        generate_markdown_report=AsyncMock(
            return_value=("官方 Markdown 总结", "源群总结正文")
        ),
        generate_image_report=AsyncMock(return_value=(None, None)),
        generate_html_report=AsyncMock(return_value=(None, None)),
    )
    dispatcher = ReportDispatcher(config, generator, sender)
    dispatcher.serverchan_sender.send_summary = AsyncMock(return_value=True)
    dispatcher.set_html_render(AsyncMock())
    dispatcher._try_upload_image = AsyncMock()
    trace = TraceContext(group_id="source-group", platform="source-platform")
    token = _current_trace.set(trace)
    yield SimpleNamespace(
        settings=settings,
        config=config,
        source=source,
        target=target,
        official=official,
        manager=bot_manager,
        sender=sender,
        generator=generator,
        dispatcher=dispatcher,
        trace=trace,
        data_dir=tmp_path,
    )
    _current_trace.reset(token)


@pytest.mark.parametrize(
    ("stop_source", "forward", "official", "serverchan"),
    list(product([False, True], repeat=4)),
)
def test_switch_combinations_deliver_only_to_enabled_destinations(
    delivery_rig, stop_source, forward, official, serverchan
):
    rig = delivery_rig
    rig.settings.update(
        stop_source_group=stop_source,
        forward_group_enabled=forward,
        qq_official_enabled=official,
        serverchan_enabled=serverchan,
    )
    assert (
        asyncio.run(rig.dispatcher.dispatch("source-group", {}, "source-platform"))
        is True
    )
    source_enabled = not (stop_source or forward or official or serverchan)
    assert rig.source.send_text_report.await_count == int(source_enabled)
    assert rig.target.send_text_report.await_count == int(forward)
    assert rig.official.send_text_report.await_count == int(official)
    assert rig.dispatcher.serverchan_sender.send_summary.await_count == int(serverchan)
    rig.sender.send_text.assert_not_awaited()
    if official:
        call = rig.official.send_text_report.await_args
        assert call.args == ("official-group", "官方 Markdown 总结")
        assert "源群总结正文" in call.kwargs["fallback_content"]
        assert (
            rig.generator.generate_markdown_report.await_args.kwargs["mention_style"]
            == "name"
        )
    if stop_source and not (forward or official or serverchan):
        assert rig.config.get_report_delivery_mode() == "disabled"
        assert rig.trace.metadata["report_delivery_skipped"] is True
        assert rig.trace.metadata["report_delivery_results"] == {}
        rig.generator.generate_text_report.assert_not_called()
        rig.generator.generate_markdown_report.assert_not_awaited()


@pytest.mark.parametrize("key", ["qq_official_enabled", "stop_source_group"])
@pytest.mark.parametrize("value", ["true", "false", 0, 1, None])
def test_new_switches_require_boolean_values(delivery_rig, key, value):
    delivery_rig.settings[key] = value
    with pytest.raises(ValueError, match="布尔值"):
        delivery_rig.config.get_report_delivery_mode()
    assert (
        asyncio.run(
            delivery_rig.dispatcher.dispatch("source-group", {}, "source-platform")
        )
        is False
    )
    delivery_rig.source.send_text_report.assert_not_awaited()
    delivery_rig.official.send_text_report.assert_not_awaited()


@pytest.mark.parametrize(
    "target",
    [
        "",
        " ",
        None,
        123,
        "123456",
        "official-main:UnknownMessage:123",
        ":GroupMessage:123",
        "official-main:GroupMessage:",
        "official-main:GroupMessage:123:456",
    ],
)
def test_official_target_requires_a_complete_group_session(delivery_rig, target):
    rig = delivery_rig
    rig.settings.update(qq_official_enabled=True, qq_official_target=target)
    assert (
        asyncio.run(rig.dispatcher.dispatch("source-group", {}, "source-platform"))
        is False
    )
    rig.source.send_text_report.assert_not_awaited()
    rig.official.send_text_report.assert_not_awaited()
    rig.sender.send_text.assert_not_awaited()


def test_official_target_uses_the_named_instance_and_normalizes_case(delivery_rig):
    rig = delivery_rig
    other = SimpleNamespace(
        get_platform_name=Mock(return_value="qq_official"),
        send_text_report=AsyncMock(return_value=True),
    )
    rig.manager._adapters["official-other"] = other
    rig.settings.update(
        qq_official_enabled=True,
        qq_official_target=" OFFICIAL-OTHER:GroupMessage: group-openid ",
    )
    assert (
        asyncio.run(rig.dispatcher.dispatch("source-group", {}, "source-platform"))
        is True
    )
    assert other.send_text_report.await_args.args[0] == "group-openid"
    rig.official.send_text_report.assert_not_awaited()
    rig.source.send_text_report.assert_not_awaited()


@pytest.mark.parametrize(
    "target",
    [
        "missing:GroupMessage:123",
        "target-platform:GroupMessage:123",
        "official-main:GroupMessage:source-group",
    ],
)
def test_official_missing_wrong_platform_or_source_group_is_rejected(
    delivery_rig, target
):
    rig = delivery_rig
    rig.settings.update(qq_official_enabled=True, qq_official_target=target)
    source_platform = (
        "official-main" if target.endswith("source-group") else "source-platform"
    )
    assert (
        asyncio.run(rig.dispatcher.dispatch("source-group", {}, source_platform))
        is False
    )
    rig.source.send_text_report.assert_not_awaited()
    rig.official.send_text_report.assert_not_awaited()
    rig.sender.send_text.assert_not_awaited()


def test_official_protocol_alias_cannot_select_an_arbitrary_instance(delivery_rig):
    rig = delivery_rig
    rig.manager.get_adapter = Mock(return_value=rig.official)
    with pytest.raises(ValueError, match="实际平台实例 ID"):
        rig.manager.validate_qq_official_report_target(
            GroupReportTarget("official-group", "qq_official"),
            "source-group",
            "source-platform",
        )


@pytest.mark.parametrize(
    "failure", ["invalid_target", "offline", "rejected", "exception"]
)
def test_official_failure_does_not_block_other_channels(delivery_rig, failure):
    rig = delivery_rig
    rig.settings.update(
        stop_source_group=True,
        forward_group_enabled=True,
        qq_official_enabled=True,
        serverchan_enabled=True,
    )
    if failure == "invalid_target":
        rig.settings["qq_official_target"] = ""
    elif failure == "offline":
        del rig.manager._adapters["official-main"]
    elif failure == "rejected":
        rig.official.send_text_report.return_value = False
    else:
        rig.official.send_text_report.side_effect = RuntimeError("模拟发送异常")
    assert (
        asyncio.run(rig.dispatcher.dispatch("source-group", {}, "source-platform"))
        is True
    )
    rig.target.send_text_report.assert_awaited_once()
    rig.dispatcher.serverchan_sender.send_summary.assert_awaited_once()
    rig.source.send_text_report.assert_not_awaited()
    assert rig.trace.metadata["report_delivery_results"] == {
        "group": True,
        "qq_official": False,
        "serverchan": True,
    }
    for call in rig.sender.send_text.await_args_list:
        assert call.args[0] == "official-group"
        assert call.args[2] == "official-main"


@pytest.mark.parametrize("failure", ["invalid_target", "rejected", "exception"])
def test_other_channel_failures_do_not_block_official_delivery(delivery_rig, failure):
    rig = delivery_rig
    rig.settings.update(
        forward_group_enabled=True, qq_official_enabled=True, serverchan_enabled=True
    )
    if failure == "invalid_target":
        rig.settings["forward_target"] = ""
        rig.settings["serverchan_chat_id"] = "invalid"
    elif failure == "rejected":
        rig.target.send_text_report.return_value = False
        rig.dispatcher.serverchan_sender.send_summary.return_value = False
    else:
        rig.target.send_text_report.side_effect = RuntimeError("模拟群发送异常")
        rig.dispatcher.serverchan_sender.send_summary.side_effect = RuntimeError(
            "模拟推送异常"
        )
    assert (
        asyncio.run(rig.dispatcher.dispatch("source-group", {}, "source-platform"))
        is True
    )
    rig.official.send_text_report.assert_awaited_once()
    rig.source.send_text_report.assert_not_awaited()
    assert rig.trace.metadata["report_delivery_results"] == {
        "group": False,
        "qq_official": True,
        "serverchan": False,
    }


@pytest.mark.parametrize("output_format", ["image", "html"])
def test_official_format_fallback_stays_at_official_target(delivery_rig, output_format):
    rig = delivery_rig
    rig.settings["qq_official_enabled"] = True
    rig.config.config["basic"]["output_format"] = [output_format]
    path = rig.data_dir / f"source-group.{output_format}"
    path.write_bytes(b"report")
    rig.generator.generate_image_report.return_value = (str(path), None)
    rig.generator.generate_html_report.return_value = (str(path), None)
    assert (
        asyncio.run(rig.dispatcher.dispatch("source-group", {}, "source-platform"))
        is True
    )
    rig.official.send_text_report.assert_awaited_once()
    rig.source.send_text_report.assert_not_awaited()
    if output_format == "image":
        assert rig.generator.generate_image_report.await_args.args[1] == "source-group"
        assert rig.sender.send_image_smart.await_args.args[0] == "official-group"
        assert rig.sender.send_image_smart.await_args.args[3] == "official-main"
    else:
        assert rig.generator.generate_html_report.await_args.args[1] == "source-group"
        assert rig.sender.send_file.await_args.args[0] == "official-group"
        assert rig.sender.send_file.await_args.kwargs["platform_id"] == "official-main"


@pytest.mark.parametrize("mode", ["disabled", "official", "all"])
@pytest.mark.parametrize("success", [False, True])
def test_manual_delivery_never_yields_the_summary_body_to_source(
    delivery_rig, mode, success
):
    rig = delivery_rig
    rig.settings.update(
        stop_source_group=True,
        forward_group_enabled=mode == "all",
        qq_official_enabled=mode != "disabled",
        serverchan_enabled=mode == "all",
    )
    rig.target.send_text_report.return_value = success
    rig.official.send_text_report.return_value = success
    rig.dispatcher.serverchan_sender.send_summary.return_value = success
    comic_handler = SimpleNamespace(try_trigger_comic_generation=Mock())
    handler = AnalysisCommandHandler(
        rig.config,
        rig.manager,
        None,
        rig.generator,
        AsyncMock(),
        report_dispatcher=rig.dispatcher,
    )
    handler.comic_handler = comic_handler
    event = SimpleNamespace(plain_result=lambda text: text)
    result = {
        "success": True,
        "group_id": "source-group",
        "platform_id": "source-platform",
        "analysis_result": {},
        "adapter": rig.source,
    }

    async def send():
        return [
            message async for message in handler.send_analysis_report(event, result)
        ]

    messages = asyncio.run(send())
    rig.source.send_text_report.assert_not_awaited()
    assert result["analysis_success"] is True
    assert all("源群总结正文" not in message for message in messages)
    if mode == "disabled":
        assert result["success"] is True
        assert result["report_sent"] is False
        assert result["report_delivery_skipped"] is True
        assert result["reason"] == "report_delivery_disabled"
        comic_handler.try_trigger_comic_generation.assert_not_called()
        rig.generator.generate_text_report.assert_not_called()
        rig.official.send_text_report.assert_not_awaited()
    else:
        assert result["report_sent"] is success
        assert result["success"] is success
        if not success:
            assert result["reason"] == "report_delivery_failed"
            assert "未发送到原群" in messages[0]


@pytest.mark.parametrize("incremental", [False, True])
def test_scheduler_keeps_analysis_success_when_delivery_is_disabled(
    delivery_rig, incremental
):
    rig = delivery_rig
    rig.settings["stop_source_group"] = True
    scheduler = object.__new__(AutoScheduler)
    scheduler._terminating = False
    scheduler.config_manager = rig.config
    scheduler.plugin_instance = None
    scheduler._get_group_name_safe = AsyncMock(return_value="测试群")
    scheduler.bot_manager = SimpleNamespace(
        is_ready_for_auto_analysis=Mock(return_value=True)
    )
    scheduler.analysis_service = SimpleNamespace(
        execute_daily_analysis=AsyncMock(
            return_value={"success": True, "analysis_result": {}}
        ),
        execute_incremental_final_report=AsyncMock(
            return_value={"success": True, "analysis_result": {}}
        ),
        incremental_store=None,
    )
    scheduler.report_dispatcher = rig.dispatcher
    execute = (
        scheduler._perform_incremental_final_report_for_group
        if incremental
        else scheduler._perform_auto_analysis_for_group
    )

    async def run():
        result = await execute("source-group", "source-platform")
        return result, TraceContext.current().status

    result, trace_status = asyncio.run(run())
    assert result["success"] is True
    assert result["analysis_success"] is True
    assert result["report_sent"] is False
    assert result["report_delivery_skipped"] is True
    assert result["reason"] == "report_delivery_disabled"
    assert trace_status == "succeeded"
    rig.source.send_text_report.assert_not_awaited()
    rig.generator.generate_text_report.assert_not_called()


@pytest.mark.parametrize("destination", ["source_group", "forward_group", "serverchan"])
def test_old_destination_migration_preserves_stop_switch_and_official_default(
    delivery_rig, destination
):
    rig = delivery_rig
    rig.settings.update(destination=destination, stop_source_group=True)
    rig.config.save_config = Mock()
    migrator = ConfigMigrator(rig.config)
    assert migrator.migrate_legacy_configs() is True
    assert rig.settings["stop_source_group"] is True
    assert rig.settings["qq_official_enabled"] is False
    expected = "disabled" if destination == "source_group" else destination
    assert rig.config.get_report_delivery_mode() == expected
    assert migrator.migrate_legacy_configs() is False


@pytest.mark.parametrize("scene", ["c2c", "guild"])
def test_private_target_preserves_user_scene_and_normalizes_instance(
    delivery_rig, scene
):
    rig = delivery_rig
    rig.settings.update(
        qq_official_enabled=True,
        qq_official_target=" OFFICIAL-MAIN:FriendMessage: source-group ",
        qq_official_private_scene=scene,
        qq_official_source_guild_id=" source-guild ",
    )
    target = rig.config.get_qq_official_report_target()
    validated = rig.manager.validate_qq_official_report_target(
        target, "source-group", "official-main"
    )
    assert validated == QQOfficialPrivateTarget(
        "source-group",
        "official-main",
        scene,
        "source-guild" if scene == "guild" else "",
    )


def test_friend_session_defaults_to_c2c_without_source_guild(delivery_rig):
    rig = delivery_rig
    rig.settings["qq_official_target"] = "official-main:FriendMessage:user-openid"
    assert rig.config.get_qq_official_report_target() == QQOfficialPrivateTarget(
        "user-openid", "official-main"
    )


@pytest.mark.parametrize("scene", [None, True, 123, "unknown", ""])
def test_private_target_rejects_invalid_scene(delivery_rig, scene):
    rig = delivery_rig
    rig.settings.update(
        qq_official_target="official-main:FriendMessage:user-openid",
        qq_official_private_scene=scene,
    )
    with pytest.raises(ValueError, match="私聊场景"):
        rig.config.get_qq_official_report_target()


@pytest.mark.parametrize("guild_id", ["", " ", None, 123])
def test_guild_private_target_requires_source_guild(delivery_rig, guild_id):
    rig = delivery_rig
    rig.settings.update(
        qq_official_target="official-main:FriendMessage:guild-user",
        qq_official_private_scene="guild",
        qq_official_source_guild_id=guild_id,
    )
    with pytest.raises(ValueError, match="来源频道"):
        rig.config.get_qq_official_report_target()


@pytest.mark.parametrize("platform", ["missing", "target-platform", "qq_official"])
def test_private_target_rejects_missing_wrong_or_alias_instance(delivery_rig, platform):
    rig = delivery_rig
    rig.settings.update(
        qq_official_enabled=True,
        qq_official_target=f"{platform}:FriendMessage:user-openid",
    )
    assert (
        asyncio.run(rig.dispatcher.dispatch("source-group", {}, "source-platform"))
        is False
    )
    rig.official.send_private_text_report.assert_not_awaited()
    rig.source.send_text_report.assert_not_awaited()
    rig.sender.send_text.assert_not_awaited()


@pytest.mark.parametrize("scene", ["c2c", "guild"])
@pytest.mark.parametrize("output", ["text", "image", "html", "html_url"])
def test_private_reports_use_private_hooks_and_keep_source_archive(
    delivery_rig, scene, output
):
    rig = delivery_rig
    rig.settings.update(
        qq_official_enabled=True,
        qq_official_target="official-main:FriendMessage:user-openid",
        qq_official_private_scene=scene,
        qq_official_source_guild_id="source-guild",
    )
    target = rig.config.get_qq_official_report_target()
    html_path = rig.data_dir / "source-group.html"
    html_path.write_text("<html>源群总结</html>", encoding="utf-8")
    rig.generator.generate_image_report.return_value = (
        "https://example.com/report.png",
        None,
    )
    rig.generator.generate_html_report.return_value = (str(html_path), None)
    rig.config.get_html_only_url = Mock(return_value=output == "html_url")
    rig.config.get_html_base_url = Mock(return_value="https://reports.example.com")
    rig.config.get_html_output_dir = Mock(return_value=str(rig.data_dir))
    hook = {
        "text": rig.official.send_private_text_report,
        "image": rig.official.send_private_image,
        "html": rig.official.send_private_file,
        "html_url": rig.official.send_private_text,
    }[output]
    assert (
        asyncio.run(
            rig.dispatcher.dispatch(
                "source-group",
                {},
                "source-platform",
                output_formats=["html" if output == "html_url" else output],
            )
        )
        is True
    )
    hook.assert_awaited_once()
    assert hook.await_args.args[0] == target
    if output == "html_url":
        assert (
            "https://reports.example.com/source-group.html" in hook.await_args.args[1]
        )
        rig.official.send_private_file.assert_not_awaited()
    elif output == "html":
        assert hook.await_args.args[1:] == (str(html_path), html_path.name)
    rig.generator.generate_markdown_report.assert_not_awaited()
    rig.official.send_text_report.assert_not_awaited()
    rig.source.send_text_report.assert_not_awaited()
    rig.sender.send_text.assert_not_awaited()
    rig.sender.send_image_smart.assert_not_awaited()
    rig.sender.send_file.assert_not_awaited()
    rig.dispatcher._try_upload_image.assert_not_awaited()
    payloads = [
        span["payload"]
        for span in rig.trace._spans
        if "target_scene" in span["payload"]
    ]
    assert payloads
    assert all(payload["target_scene"] == scene for payload in payloads)
    assert all(payload["target_user_id"] == "user-openid" for payload in payloads)
    assert all("target_group_id" not in payload for payload in payloads)
    assert rig.trace.group_id == "source-group"


@pytest.mark.parametrize("scene", ["c2c", "guild"])
@pytest.mark.parametrize("output", ["image", "html"])
@pytest.mark.parametrize("text_success", [True, False])
def test_private_attachment_failure_falls_back_only_to_same_private_target(
    delivery_rig, scene, output, text_success
):
    rig = delivery_rig
    rig.settings.update(
        qq_official_enabled=True,
        qq_official_target="official-main:FriendMessage:user-openid",
        qq_official_private_scene=scene,
        qq_official_source_guild_id="source-guild",
    )
    target = rig.config.get_qq_official_report_target()
    html_path = rig.data_dir / "source-group.html"
    html_path.write_text("报告", encoding="utf-8")
    rig.generator.generate_image_report.return_value = (
        "https://example.com/report.png",
        None,
    )
    rig.generator.generate_html_report.return_value = (str(html_path), None)
    rig.official.send_private_image.return_value = False
    rig.official.send_private_file.return_value = False
    rig.official.send_private_text_report.return_value = text_success
    assert (
        asyncio.run(
            rig.dispatcher.dispatch(
                "source-group",
                {},
                "source-platform",
                output_formats=[output],
            )
        )
        is text_success
    )
    attachment_hook = (
        rig.official.send_private_image
        if output == "image"
        else rig.official.send_private_file
    )
    attachment_hook.assert_awaited_once()
    assert attachment_hook.await_args.args[0] == target
    rig.official.send_private_text_report.assert_awaited_once_with(
        target, "源群总结正文"
    )
    rig.official.send_text_report.assert_not_awaited()
    rig.source.send_text_report.assert_not_awaited()
    rig.sender.send_text.assert_not_awaited()
    rig.sender.send_image_smart.assert_not_awaited()
    rig.sender.send_file.assert_not_awaited()
    rig.dispatcher._try_upload_image.assert_not_awaited()


@pytest.mark.parametrize("scene", ["c2c", "guild"])
@pytest.mark.parametrize("failure", ["rejected", "exception"])
def test_private_failure_does_not_block_other_channels_or_send_to_groups(
    delivery_rig, scene, failure
):
    rig = delivery_rig
    rig.settings.update(
        forward_group_enabled=True,
        qq_official_enabled=True,
        serverchan_enabled=True,
        qq_official_target="official-main:FriendMessage:user-openid",
        qq_official_private_scene=scene,
        qq_official_source_guild_id="source-guild",
    )
    if failure == "exception":
        rig.official.send_private_text_report.side_effect = RuntimeError("模拟私聊拒绝")
    else:
        rig.official.send_private_text_report.return_value = False
    assert (
        asyncio.run(rig.dispatcher.dispatch("source-group", {}, "source-platform"))
        is True
    )
    assert rig.trace.metadata["report_delivery_results"] == {
        "group": True,
        "qq_official": False,
        "serverchan": True,
    }
    rig.target.send_text_report.assert_awaited_once()
    rig.dispatcher.serverchan_sender.send_summary.assert_awaited_once()
    rig.official.send_text_report.assert_not_awaited()
    rig.source.send_text_report.assert_not_awaited()
    rig.sender.send_text.assert_not_awaited()
