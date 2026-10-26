"""总结投递配置、机器人接口和禁止回发原群的回归测试。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from src.application.handlers.analysis_command_handler import AnalysisCommandHandler
from src.domain.value_objects.report_delivery import (
    FeishuBotConfig,
    FeishuReuseTarget,
    GroupReportTarget,
    ServerChanBotConfig,
)
from src.infrastructure.config.config_manager import ConfigManager
from src.infrastructure.config.config_migrator import ConfigMigrator
from src.infrastructure.messaging import feishu_bot_sender, serverchan_bot_sender
from src.infrastructure.messaging.feishu_bot_sender import FeishuBotSender
from src.infrastructure.messaging.serverchan_bot_sender import ServerChanBotSender
from src.infrastructure.reporting.dispatcher import ReportDispatcher
from src.shared.trace_context import TraceContext, _current_trace


def make_config(settings=None):
    config = object.__new__(ConfigManager)
    config.config = {"report_delivery": settings or {}}
    return config


def test_delivery_defaults_to_source_group_for_existing_config():
    config = object.__new__(ConfigManager)
    config.config = {}
    assert config.get_report_delivery_mode() == "source_group"


@pytest.mark.parametrize("mode", ["source_group", "forward_group", "serverchan"])
def test_delivery_mode_accepts_supported_destinations(mode):
    assert make_config({"destination": mode}).get_report_delivery_mode() == mode


@pytest.mark.parametrize("mode", ["", "other", None, True])
def test_invalid_delivery_mode_is_rejected(mode):
    with pytest.raises(ValueError, match="投递方式无效"):
        make_config({"destination": mode}).get_report_delivery_mode()


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (" 654321 ", GroupReportTarget("654321", "onebot-main")),
        ("other:GroupMessage:654321", GroupReportTarget("654321", "other")),
        ("other:GroupMessage:123456", GroupReportTarget("123456", "other")),
        (
            "telegram:GroupMessage:-100123:456",
            GroupReportTarget("-100123:456", "telegram"),
        ),
    ],
)
def test_forward_target_uses_source_platform_or_explicit_session(target, expected):
    assert (
        make_config({"forward_target": target}).get_forward_report_target(
            "123456", "onebot-main"
        )
        == expected
    )


@pytest.mark.parametrize(
    "target",
    [
        "",
        " ",
        None,
        654321,
        "other:FriendMessage:654321",
        ":GroupMessage:654321",
        "other:GroupMessage:",
        "other:654321",
        "123456",
        "ONEBOT-MAIN:GroupMessage:123456",
    ],
)
def test_invalid_or_source_group_forward_target_is_rejected(target):
    with pytest.raises(ValueError):
        make_config({"forward_target": target}).get_forward_report_target(
            "123456", "onebot-main"
        )


@pytest.mark.parametrize("uid", [123, "123", " 00123 "])
def test_serverchan_config_accepts_positive_uid_and_hides_token(uid):
    config = make_config(
        {
            "serverchan_bot_token": " secret-token ",
            "serverchan_chat_id": uid,
            "serverchan_silent": True,
        }
    ).get_serverchan_bot_config()
    assert config == ServerChanBotConfig("secret-token", 123, True)
    assert "secret-token" not in repr(config)


@pytest.mark.parametrize(
    "uid", ["", "0", "-1", "1.5", "１２３", "abc", 0, -1, True, None]
)
def test_serverchan_config_rejects_invalid_uid(uid):
    with pytest.raises(ValueError, match="UID 必须是正整数"):
        make_config(
            {"serverchan_bot_token": "secret-token", "serverchan_chat_id": uid}
        ).get_serverchan_bot_config()


@pytest.mark.parametrize("token", ["", " ", None, 123])
def test_serverchan_config_requires_bot_token(token):
    with pytest.raises(ValueError, match="Bot Token 不能为空"):
        make_config(
            {"serverchan_bot_token": token, "serverchan_chat_id": "123"}
        ).get_serverchan_bot_config()


def test_serverchan_config_rejects_nonboolean_silent_flag():
    with pytest.raises(ValueError, match="静默推送必须是布尔值"):
        make_config(
            {
                "serverchan_bot_token": "token",
                "serverchan_chat_id": "123",
                "serverchan_silent": "false",
            }
        ).get_serverchan_bot_config()


def stub_http(monkeypatch, *, status=200, body=None, error=None):
    response = MagicMock()
    response.status = status
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    response.json = AsyncMock(return_value=body, side_effect=error)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.post.return_value = response
    factory = Mock(return_value=session)
    monkeypatch.setattr(serverchan_bot_sender.aiohttp, "ClientSession", factory)
    return factory, session, response


def test_serverchan_sends_documented_payload_without_redirects(monkeypatch):
    factory, session, response = stub_http(monkeypatch, body={"ok": True})
    assert (
        asyncio.run(
            ServerChanBotSender().send_summary(
                ServerChanBotConfig("token/secret", 123, True), "总结正文"
            )
        )
        is True
    )
    session.post.assert_called_once_with(
        "https://bot-go.apijia.cn/bottoken%2Fsecret/sendMessage",
        json={
            "chat_id": 123,
            "text": "总结正文",
            "parse_mode": "markdown",
            "silent": True,
        },
        allow_redirects=False,
    )
    assert factory.call_args.kwargs["timeout"].total == 30
    response.json.assert_awaited_once()


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, {"ok": True}),
        (302, {"ok": True}),
        (500, {"ok": True}),
        (200, {"ok": False}),
        (200, {"ok": "true"}),
        (200, {}),
        (200, None),
        (200, []),
    ],
)
def test_serverchan_does_not_treat_rejected_or_invalid_responses_as_success(
    monkeypatch, status, body
):
    _, _, response = stub_http(monkeypatch, status=status, body=body)
    assert (
        asyncio.run(
            ServerChanBotSender().send_summary(
                ServerChanBotConfig("secret", 123), "正文"
            )
        )
        is False
    )
    if not 200 <= status < 300:
        response.json.assert_not_awaited()


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("URL contains secret-token"),
        ValueError("invalid JSON secret-token"),
    ],
)
def test_serverchan_errors_do_not_log_token_or_response(monkeypatch, error):
    stub_http(monkeypatch, error=error)
    log = Mock()
    monkeypatch.setattr(serverchan_bot_sender, "logger", log)
    assert (
        asyncio.run(
            ServerChanBotSender().send_summary(
                ServerChanBotConfig("secret-token", 123), "正文"
            )
        )
        is False
    )
    log.error.assert_called_once_with(
        f"[Server酱机器人] 推送异常：{type(error).__name__}"
    )
    assert "secret-token" not in repr(log.mock_calls)


def test_serverchan_empty_summary_does_not_start_request(monkeypatch):
    factory, _, _ = stub_http(monkeypatch, body={"ok": True})
    assert (
        asyncio.run(
            ServerChanBotSender().send_summary(
                ServerChanBotConfig("secret", 123), " \n "
            )
        )
        is False
    )
    factory.assert_not_called()


def make_dispatcher(
    tmp_path, *, mode="forward_group", output_format="text", target_group="target-group"
):
    source_adapter = SimpleNamespace(
        get_platform_name=Mock(return_value="onebot"),
        get_group_info=AsyncMock(return_value=None),
        send_text_report=AsyncMock(return_value=True),
    )
    target_adapter = SimpleNamespace(
        get_platform_name=Mock(return_value="onebot"),
        send_text_report=AsyncMock(return_value=False),
    )
    adapters = {"source-platform": source_adapter, "target-platform": target_adapter}
    bot_manager = SimpleNamespace(
        get_adapter=Mock(side_effect=adapters.get),
        get_adapter_platform_id=Mock(
            side_effect=lambda adapter: (
                "source-platform" if adapter is source_adapter else "target-platform"
            )
        ),
    )
    message_sender = SimpleNamespace(
        bot_manager=bot_manager,
        send_text=AsyncMock(return_value=False),
        send_file=AsyncMock(return_value=False),
        send_image_smart=AsyncMock(return_value=False),
    )
    config = make_config(
        {
            "destination": mode,
            "forward_target": f"target-platform:GroupMessage:{target_group}",
            "serverchan_bot_token": "secret-token",
            "serverchan_chat_id": "123",
            "feishu_app_id": "cli_xxx",
            "feishu_app_secret": "secret",
            "feishu_user_id": "ou_abc",
        }
    )
    config.get_output_format = Mock(return_value=[output_format])
    config.get_report_template = Mock(return_value="scrapbook")
    config.get_show_report_caption = Mock(return_value=False)
    config.get_html_only_url = Mock(return_value=False)
    config.get_html_base_url = Mock(return_value="")
    generator = SimpleNamespace(
        data_dir=tmp_path,
        generate_text_report=Mock(return_value="源群总结正文"),
        generate_image_report=AsyncMock(return_value=(None, None)),
        generate_html_report=AsyncMock(return_value=(None, None)),
    )
    dispatcher = ReportDispatcher(config, generator, message_sender)
    dispatcher.set_html_render(AsyncMock())
    dispatcher._try_upload_image = AsyncMock()
    return dispatcher, source_adapter, target_adapter


@pytest.mark.parametrize("same_object", [True, False])
def test_forward_cannot_resolve_platform_alias_back_to_source_group(
    tmp_path, same_object
):
    dispatcher, source, target = make_dispatcher(tmp_path, target_group="source-group")
    if same_object:
        dispatcher.message_sender.bot_manager.get_adapter.side_effect = None
        dispatcher.message_sender.bot_manager.get_adapter.return_value = source
    else:
        dispatcher.message_sender.bot_manager.get_adapter_platform_id.side_effect = None
        dispatcher.message_sender.bot_manager.get_adapter_platform_id.return_value = (
            "real-platform-id"
        )
    assert (
        asyncio.run(dispatcher.dispatch("source-group", {}, "source-platform")) is False
    )
    source.send_text_report.assert_not_awaited()
    target.send_text_report.assert_not_awaited()
    dispatcher.message_sender.send_text.assert_not_awaited()


def test_same_group_identifier_on_distinct_platforms_is_allowed(tmp_path):
    dispatcher, source, target = make_dispatcher(tmp_path, target_group="source-group")
    target.send_text_report.return_value = True
    assert (
        asyncio.run(dispatcher.dispatch("source-group", {}, "source-platform")) is True
    )
    target.send_text_report.assert_awaited_once_with(
        "source-group", "来源群：source-group\n\n源群总结正文"
    )
    source.send_text_report.assert_not_awaited()


@pytest.mark.parametrize("output_format", ["image", "html"])
def test_forward_format_failure_and_text_fallback_only_use_target(
    tmp_path, output_format
):
    dispatcher, source, target = make_dispatcher(tmp_path, output_format=output_format)
    report_path = tmp_path / f"source-group.{output_format}"
    report_path.write_bytes(b"report")
    generator = dispatcher.report_generator
    generator.generate_image_report.return_value = (str(report_path), None)
    generator.generate_html_report.return_value = (str(report_path), None)

    assert (
        asyncio.run(dispatcher.dispatch("source-group", {}, "source-platform")) is False
    )
    source.send_text_report.assert_not_awaited()
    target.send_text_report.assert_awaited_once_with(
        "target-group", "来源群：source-group\n\n源群总结正文"
    )
    assert dispatcher.message_sender.send_text.await_args.args[0] == "target-group"
    assert dispatcher.message_sender.send_text.await_args.args[2] == "target-platform"
    if output_format == "image":
        assert generator.generate_image_report.await_args.args[1] == "source-group"
        assert (
            generator.generate_image_report.await_args.kwargs["avatar_cache_namespace"]
            == "source-platform"
        )
        dispatcher.message_sender.send_image_smart.assert_awaited_once_with(
            "target-group",
            str(report_path),
            "来源群：source-group",
            "target-platform",
        )
        dispatcher._try_upload_image.assert_awaited_once_with(
            "target-group", str(report_path), "target-platform"
        )
        assert next((tmp_path / "reports").glob("report_source-group_*.jpg"), None)
    else:
        assert generator.generate_html_report.await_args.args[1] == "source-group"
        assert (
            generator.generate_html_report.await_args.kwargs["avatar_cache_namespace"]
            == "source-platform"
        )
        dispatcher.message_sender.send_file.assert_awaited_once_with(
            "target-group",
            str(report_path),
            caption="来源群：source-group",
            platform_id="target-platform",
        )


def test_forward_html_link_only_uses_target(tmp_path):
    dispatcher, source, target = make_dispatcher(tmp_path, output_format="html")
    report_path = tmp_path / "source-group.html"
    report_path.write_text("report", encoding="utf-8")
    dispatcher.report_generator.generate_html_report.return_value = (
        str(report_path),
        None,
    )
    dispatcher.config_manager.get_html_only_url.return_value = True
    dispatcher.config_manager.get_html_base_url.return_value = (
        "https://reports.example.test"
    )
    dispatcher.config_manager.get_html_output_dir = Mock(return_value=str(tmp_path))
    dispatcher.message_sender.send_text.return_value = True
    assert (
        asyncio.run(dispatcher.dispatch("source-group", {}, "source-platform")) is True
    )
    args = dispatcher.message_sender.send_text.await_args.args
    assert args[0] == "target-group"
    assert "https://reports.example.test/source-group.html" in args[1]
    assert args[2] == "target-platform"
    source.send_text_report.assert_not_awaited()
    target.send_text_report.assert_not_awaited()
    dispatcher.message_sender.send_file.assert_not_awaited()


@pytest.mark.parametrize("mode", ["forward_group", "serverchan", "both"])
@pytest.mark.parametrize("success", [True, False])
def test_manual_summary_uses_dispatcher_without_sending_body_to_source(
    tmp_path, mode, success
):
    dispatcher, source, target = make_dispatcher(
        tmp_path, mode="forward_group" if mode == "both" else mode
    )
    if mode == "both":
        dispatcher.config_manager.config["report_delivery"].update(
            destination="independent",
            forward_group_enabled=True,
            serverchan_enabled=True,
        )
    target.send_text_report.return_value = success
    dispatcher.serverchan_sender.send_summary = AsyncMock(return_value=success)
    handler = AnalysisCommandHandler(
        dispatcher.config_manager,
        dispatcher.message_sender.bot_manager,
        None,
        dispatcher.report_generator,
        AsyncMock(),
        report_dispatcher=dispatcher,
    )
    event = SimpleNamespace(plain_result=Mock(side_effect=lambda text: text))
    result = {
        "success": True,
        "group_id": "source-group",
        "platform_id": "source-platform",
        "analysis_result": {},
        "adapter": source,
    }

    async def scenario():
        return [
            message async for message in handler.send_analysis_report(event, result)
        ]

    messages = asyncio.run(scenario())
    source.send_text_report.assert_not_awaited()
    assert result["analysis_success"] is True
    assert result["report_sent"] is success
    if success:
        assert result["success"] is True
        assert messages == []
    else:
        assert result["success"] is False
        assert result["reason"] == "report_delivery_failed"
        assert len(messages) == 1
        assert "未发送到原群" in messages[0]
        assert "源群总结正文" not in messages[0]
    if mode == "serverchan":
        dispatcher.serverchan_sender.send_summary.assert_awaited_once()
        target.send_text_report.assert_not_awaited()
        dispatcher.message_sender.send_text.assert_not_awaited()


def test_invalid_forward_config_does_not_call_any_sender(tmp_path):
    dispatcher, source, target = make_dispatcher(tmp_path)
    dispatcher.config_manager.config["report_delivery"]["forward_target"] = ""
    assert (
        asyncio.run(dispatcher.dispatch("source-group", {}, "source-platform")) is False
    )
    source.send_text_report.assert_not_awaited()
    target.send_text_report.assert_not_awaited()
    dispatcher.message_sender.send_text.assert_not_awaited()


@pytest.mark.parametrize(
    ("forward", "serverchan", "mode"),
    [
        (False, False, "source_group"),
        (True, False, "forward_group"),
        (False, True, "serverchan"),
        (True, True, "forward_group_and_serverchan"),
    ],
)
def test_independent_switches_resolve_all_delivery_combinations(
    forward, serverchan, mode
):
    config = make_config(
        {
            "destination": "independent",
            "forward_group_enabled": forward,
            "serverchan_enabled": serverchan,
        }
    )
    assert config.get_report_delivery_mode() == mode


@pytest.mark.parametrize("key", ["forward_group_enabled", "serverchan_enabled"])
@pytest.mark.parametrize("value", [None, "true", "false", 0, 1])
def test_independent_switches_require_boolean_values(key, value):
    with pytest.raises(ValueError, match="开关必须是布尔值"):
        make_config({key: value}).get_report_delivery_mode()


@pytest.mark.parametrize("schema_defaults_added", [False, True])
@pytest.mark.parametrize(
    ("legacy", "forward", "serverchan"),
    [
        ("source_group", False, False),
        ("forward_group", True, False),
        ("serverchan", False, True),
    ],
)
def test_legacy_destination_migrates_once_and_preserves_user_settings(
    schema_defaults_added, legacy, forward, serverchan
):
    settings = {
        "destination": legacy,
        "forward_target": "other:GroupMessage:654321",
        "serverchan_bot_token": "secret-token",
        "serverchan_chat_id": "123",
        "serverchan_silent": True,
    }
    if schema_defaults_added:
        settings.update(forward_group_enabled=False, serverchan_enabled=False)
    config = make_config(settings)
    config.save_config = Mock()
    migrator = ConfigMigrator(config)

    assert migrator.migrate_legacy_configs() is True
    config.save_config.assert_called_once_with()
    assert settings["destination"] == "independent"
    assert settings["forward_group_enabled"] is forward
    assert settings["serverchan_enabled"] is serverchan
    assert config.get_forward_report_target("123456", "source") == GroupReportTarget(
        "654321", "other"
    )
    assert config.get_serverchan_bot_config() == ServerChanBotConfig(
        "secret-token", 123, True
    )

    settings.update(
        forward_group_enabled=not forward, serverchan_enabled=not serverchan
    )
    changed_settings = settings.copy()
    config.save_config.reset_mock()
    assert migrator.migrate_legacy_configs() is False
    assert settings == changed_settings
    config.save_config.assert_not_called()


def test_fresh_independent_config_does_not_need_migration():
    config = make_config({"destination": "independent"})
    config.save_config = Mock()
    assert ConfigMigrator(config).migrate_legacy_configs() is False
    assert config.get_report_delivery_mode() == "source_group"
    config.save_config.assert_not_called()


@pytest.mark.parametrize("forward", [False, True])
@pytest.mark.parametrize("serverchan", [False, True])
def test_independent_switches_send_only_to_enabled_destinations(
    tmp_path, forward, serverchan
):
    dispatcher, source, target = make_dispatcher(tmp_path)
    dispatcher.config_manager.config["report_delivery"].update(
        destination="independent",
        forward_group_enabled=forward,
        serverchan_enabled=serverchan,
    )
    target.send_text_report.return_value = True
    dispatcher.serverchan_sender.send_summary = AsyncMock(return_value=True)

    assert (
        asyncio.run(dispatcher.dispatch("source-group", {}, "source-platform")) is True
    )
    if forward:
        target.send_text_report.assert_awaited_once_with(
            "target-group", "来源群：source-group\n\n源群总结正文"
        )
    else:
        target.send_text_report.assert_not_awaited()
    if forward or serverchan:
        source.send_text_report.assert_not_awaited()
    else:
        source.send_text_report.assert_awaited_once_with("source-group", "源群总结正文")
    assert dispatcher.serverchan_sender.send_summary.await_count == int(serverchan)
    dispatcher.message_sender.send_text.assert_not_awaited()


@pytest.mark.parametrize("group_outcome", ["success", "failure", "exception"])
@pytest.mark.parametrize("bot_outcome", ["success", "failure", "exception"])
def test_dual_delivery_attempts_both_channels_and_records_partial_results(
    tmp_path, group_outcome, bot_outcome
):
    dispatcher, source, target = make_dispatcher(tmp_path)
    dispatcher.config_manager.config["report_delivery"].update(
        destination="independent", forward_group_enabled=True, serverchan_enabled=True
    )
    group_ok = group_outcome == "success"
    bot_ok = bot_outcome == "success"
    target.send_text_report.return_value = group_ok
    if group_outcome == "exception":
        target.send_text_report.side_effect = RuntimeError("群平台不可用")
    dispatcher.serverchan_sender.send_summary = AsyncMock(return_value=bot_ok)
    if bot_outcome == "exception":
        dispatcher.serverchan_sender.send_summary.side_effect = TimeoutError(
            "包含 secret-token 的请求地址"
        )
    trace = TraceContext(group_id="source-group", platform="source-platform")
    token = _current_trace.set(trace)
    try:
        sent = asyncio.run(dispatcher.dispatch("source-group", {}, "source-platform"))
    finally:
        _current_trace.reset(token)

    assert sent is (group_ok or bot_ok)
    target.send_text_report.assert_awaited_once_with(
        "target-group", "来源群：source-group\n\n源群总结正文"
    )
    dispatcher.serverchan_sender.send_summary.assert_awaited_once()
    assert trace.metadata["report_delivery_results"] == {
        "group": group_ok,
        "serverchan": bot_ok,
    }
    source.send_text_report.assert_not_awaited()
    assert all(
        call.args[0] == "target-group"
        for call in dispatcher.message_sender.send_text.await_args_list
    )


@pytest.mark.parametrize(
    "invalid_target", ["", "other:FriendMessage:123", "source-group"]
)
def test_invalid_forward_target_does_not_block_enabled_serverchan(
    tmp_path, invalid_target
):
    dispatcher, source, target = make_dispatcher(tmp_path)
    dispatcher.config_manager.config["report_delivery"].update(
        destination="independent",
        forward_group_enabled=True,
        serverchan_enabled=True,
        forward_target=invalid_target,
    )
    dispatcher.serverchan_sender.send_summary = AsyncMock(return_value=True)

    assert (
        asyncio.run(dispatcher.dispatch("source-group", {}, "source-platform")) is True
    )
    dispatcher.serverchan_sender.send_summary.assert_awaited_once()
    source.send_text_report.assert_not_awaited()
    target.send_text_report.assert_not_awaited()
    dispatcher.message_sender.send_text.assert_not_awaited()


@pytest.mark.parametrize(
    ("key", "value"), [("serverchan_bot_token", ""), ("serverchan_chat_id", "bad-uid")]
)
def test_invalid_serverchan_config_does_not_undo_successful_group_delivery(
    tmp_path, key, value
):
    dispatcher, source, target = make_dispatcher(tmp_path)
    settings = dispatcher.config_manager.config["report_delivery"]
    settings.update(
        destination="independent", forward_group_enabled=True, serverchan_enabled=True
    )
    settings[key] = value
    target.send_text_report.return_value = True
    dispatcher.serverchan_sender.send_summary = AsyncMock()

    assert (
        asyncio.run(dispatcher.dispatch("source-group", {}, "source-platform")) is True
    )
    target.send_text_report.assert_awaited_once_with(
        "target-group", "来源群：source-group\n\n源群总结正文"
    )
    dispatcher.serverchan_sender.send_summary.assert_not_awaited()
    source.send_text_report.assert_not_awaited()


# ================================================================
# 飞书机器人通道：配置校验、接口契约与投递回退
# ================================================================


@pytest.mark.parametrize("user_id_type", ["open_id", "user_id", "union_id"])
def test_feishu_config_accepts_credentials_and_trims_spaces(user_id_type):
    config = make_config(
        {
            "feishu_app_id": " cli_xxx ",
            "feishu_app_secret": " secret ",
            "feishu_user_id": " ou_abc ",
            "feishu_user_id_type": user_id_type,
        }
    ).get_feishu_bot_config()
    assert config == FeishuBotConfig("cli_xxx", "secret", "ou_abc", user_id_type)
    assert "secret" not in repr(config)


def test_feishu_config_defaults_to_open_id():
    config = make_config(
        {
            "feishu_app_id": "cli_xxx",
            "feishu_app_secret": "secret",
            "feishu_user_id": "ou_abc",
        }
    ).get_feishu_bot_config()
    assert config.user_id_type == "open_id"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("feishu_app_id", ""),
        ("feishu_app_id", "   "),
        ("feishu_app_id", None),
        ("feishu_app_id", 123),
        ("feishu_app_secret", ""),
        ("feishu_app_secret", "   "),
        ("feishu_app_secret", None),
        ("feishu_app_secret", 123),
        ("feishu_user_id", ""),
        ("feishu_user_id", "   "),
        ("feishu_user_id", None),
        ("feishu_user_id", 123),
    ],
)
def test_feishu_config_requires_nonempty_credentials(key, value):
    settings = {
        "feishu_app_id": "cli_xxx",
        "feishu_app_secret": "secret",
        "feishu_user_id": "ou_abc",
    }
    settings[key] = value
    with pytest.raises(ValueError, match="不能为空"):
        make_config(settings).get_feishu_bot_config()


@pytest.mark.parametrize("user_id_type", ["", "openid", "ou_", None, True, 1])
def test_feishu_config_rejects_invalid_user_id_type(user_id_type):
    with pytest.raises(ValueError, match="飞书用户 ID 类型"):
        make_config(
            {
                "feishu_app_id": "cli_xxx",
                "feishu_app_secret": "secret",
                "feishu_user_id": "ou_abc",
                "feishu_user_id_type": user_id_type,
            }
        ).get_feishu_bot_config()


_FEISHU_DEFAULT_TOKEN_BODY = {
    "code": 0,
    "tenant_access_token": "t-token",
    "expire": 7200,
}
_FEISHU_DEFAULT_UPLOAD_BODY = {"code": 0, "data": {"image_key": "img_v2_xxx"}}
_FEISHU_DEFAULT_SEND_BODY = {"code": 0, "msg": "success", "data": {"message_id": "om_xxx"}}


def stub_feishu_http(
    monkeypatch,
    *,
    token_status=200,
    token_body=_FEISHU_DEFAULT_TOKEN_BODY,
    status=200,
    body=None,
    error=None,
):
    """按顺序返回访问凭证与消息发送两个响应的 HTTP 桩。"""
    token_response = MagicMock()
    token_response.status = token_status
    token_response.__aenter__ = AsyncMock(return_value=token_response)
    token_response.__aexit__ = AsyncMock(return_value=False)
    token_response.json = AsyncMock(return_value=token_body)
    send_response = MagicMock()
    send_response.status = status
    send_response.__aenter__ = AsyncMock(return_value=send_response)
    send_response.__aexit__ = AsyncMock(return_value=False)
    send_response.json = AsyncMock(return_value=body, side_effect=error)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.post.side_effect = [token_response, send_response]
    factory = Mock(return_value=session)
    monkeypatch.setattr(feishu_bot_sender.aiohttp, "ClientSession", factory)
    return factory, session, token_response, send_response


def make_feishu_config():
    return FeishuBotConfig("cli_xxx", "secret-value", "ou_abc", "open_id")


def test_feishu_sends_documented_payload_with_tenant_token(monkeypatch):
    factory, session, _, send_response = stub_feishu_http(
        monkeypatch,
        body={"code": 0, "msg": "success", "data": {"message_id": "om_xxx"}},
    )
    assert (
        asyncio.run(FeishuBotSender().send_summary(make_feishu_config(), "总结正文"))
        is True
    )
    token_call, send_call = session.post.call_args_list
    assert token_call.args[0] == (
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    )
    assert token_call.kwargs["json"] == {
        "app_id": "cli_xxx",
        "app_secret": "secret-value",
    }
    assert send_call.args[0] == "https://open.feishu.cn/open-apis/im/v1/messages"
    assert send_call.kwargs["params"] == {"receive_id_type": "open_id"}
    assert send_call.kwargs["headers"] == {"Authorization": "Bearer t-token"}
    assert send_call.kwargs["json"] == {
        "receive_id": "ou_abc",
        "msg_type": "text",
        "content": json.dumps({"text": "总结正文"}, ensure_ascii=False),
    }
    assert factory.call_args.kwargs["timeout"].total == 30
    send_response.json.assert_awaited_once()


def test_feishu_message_uses_selected_receive_id_type(monkeypatch):
    _, session, _, _ = stub_feishu_http(
        monkeypatch,
        token_body={"code": 0, "tenant_access_token": "t-token"},
        body={"code": 0, "msg": "success", "data": {"message_id": "om_xxx"}},
    )
    config = FeishuBotConfig("cli_xxx", "secret-value", "u_abc", "user_id")
    assert asyncio.run(FeishuBotSender().send_summary(config, "正文")) is True
    assert session.post.call_args_list[1].kwargs["params"] == {
        "receive_id_type": "user_id"
    }
    assert session.post.call_args_list[1].kwargs["json"]["receive_id"] == "u_abc"


@pytest.mark.parametrize(
    ("token_status", "token_body"),
    [
        (401, {"code": 0, "tenant_access_token": "t-token"}),
        (200, {"code": 99991663, "msg": "app_secret 无效"}),
        (200, {"code": 0}),
        (200, {"code": 0, "tenant_access_token": ""}),
        (200, {"code": 0, "tenant_access_token": 123}),
        (200, None),
        (200, []),
    ],
)
def test_feishu_does_not_send_message_when_token_is_unavailable(
    monkeypatch, token_status, token_body
):
    _, session, _, _ = stub_feishu_http(
        monkeypatch, token_status=token_status, token_body=token_body
    )
    assert (
        asyncio.run(FeishuBotSender().send_summary(make_feishu_config(), "正文"))
        is False
    )
    assert session.post.call_count == 1


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (403, {"code": 0, "msg": "success"}),
        (500, {"code": 0, "msg": "success"}),
        (200, {"code": 230013, "msg": "receive_id 无效"}),
        (200, {"code": 0}),
        (200, {}),
        (200, None),
        (200, []),
    ],
)
def test_feishu_does_not_treat_rejected_or_invalid_responses_as_success(
    monkeypatch, status, body
):
    _, _, _, _ = stub_feishu_http(monkeypatch, status=status, body=body)
    assert (
        asyncio.run(FeishuBotSender().send_summary(make_feishu_config(), "正文"))
        is False
    )


def test_feishu_errors_do_not_log_credentials(monkeypatch):
    stub_feishu_http(monkeypatch, error=ValueError("invalid JSON secret-value"))
    log = Mock()
    monkeypatch.setattr(feishu_bot_sender, "logger", log)
    assert (
        asyncio.run(FeishuBotSender().send_summary(make_feishu_config(), "正文"))
        is False
    )
    log.error.assert_called_once_with("[飞书机器人] 推送异常：ValueError")
    assert "secret-value" not in repr(log.mock_calls)


def test_feishu_empty_summary_does_not_start_request(monkeypatch):
    factory, _, _, _ = stub_feishu_http(monkeypatch, body={"code": 0})
    assert (
        asyncio.run(FeishuBotSender().send_summary(make_feishu_config(), " \n "))
        is False
    )
    factory.assert_not_called()


@pytest.mark.parametrize(
    ("forward", "serverchan", "feishu", "mode"),
    [
        (False, False, True, "feishu"),
        (True, False, True, "forward_group_and_feishu"),
        (False, True, True, "serverchan_and_feishu"),
        (False, False, False, "source_group"),
        (True, True, True, "forward_group_and_serverchan_and_feishu"),
    ],
)
def test_feishu_switch_resolves_delivery_combinations(
    forward, serverchan, feishu, mode
):
    config = make_config(
        {
            "destination": "independent",
            "forward_group_enabled": forward,
            "serverchan_enabled": serverchan,
            "feishu_enabled": feishu,
        }
    )
    assert config.get_report_delivery_mode() == mode


@pytest.mark.parametrize(
    ("qq_official", "serverchan", "feishu", "mode"),
    [
        (True, False, True, "qq_official_and_feishu"),
        (True, True, True, "qq_official_and_serverchan_and_feishu"),
        (True, True, False, "qq_official_and_serverchan"),
        (True, False, False, "qq_official"),
    ],
)
def test_qq_official_switch_resolves_delivery_combinations(
    qq_official, serverchan, feishu, mode
):
    config = make_config(
        {
            "destination": "independent",
            "qq_official_enabled": qq_official,
            "serverchan_enabled": serverchan,
            "feishu_enabled": feishu,
        }
    )
    assert config.get_report_delivery_mode() == mode


def test_all_delivery_switches_require_boolean_values():
    with pytest.raises(ValueError, match="开关必须是布尔值"):
        make_config({"feishu_enabled": "true"}).get_report_delivery_mode()


# ================================================================
# 飞书复用模式：会话解析、实例校验与投递路径
# ================================================================


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (
            "lark-main:FriendMessage:ou_abc",
            FeishuReuseTarget("ou_abc", "lark-main"),
        ),
        (
            " lark-main:GroupMessage:oc_xyz ",
            GroupReportTarget("oc_xyz", "lark-main"),
        ),
    ],
)
def test_feishu_reuse_target_parses_session_id(target, expected):
    assert make_config({"feishu_target": target}).get_feishu_reuse_target() == expected


def test_feishu_reuse_target_empty_returns_none():
    assert make_config({}).get_feishu_reuse_target() is None
    assert make_config({"feishu_target": "  "}).get_feishu_reuse_target() is None


@pytest.mark.parametrize(
    "target",
    [
        "lark-main:FriendMessage:ou_abc:extra",
        "lark-main:GroupMessage",
        "lark-main:PrivateMessage:ou_abc",
        ":FriendMessage:ou_abc",
        "lark-main:FriendMessage:",
        "lark-main:FriendMessage:oc_x:y",
    ],
)
def test_invalid_feishu_reuse_target_is_rejected(target):
    with pytest.raises(ValueError, match="飞书目标必须是完整会话 ID"):
        make_config({"feishu_target": target}).get_feishu_reuse_target()


def test_feishu_reuse_mode_takes_priority_over_direct_credentials(tmp_path):
    dispatcher, source, target = make_dispatcher(tmp_path)
    dispatcher.config_manager.config["report_delivery"].update(
        destination="independent",
        feishu_enabled=True,
        feishu_target="lark-main:FriendMessage:ou_abc",
    )
    dispatcher.feishu_sender.send_summary = AsyncMock()
    bot_manager = Mock()
    bot_manager.validate_lark_report_target.side_effect = lambda t, g, p: t
    bot_manager.get_adapter.return_value = SimpleNamespace(
        send_private_text_report=AsyncMock(return_value=True)
    )
    dispatcher.message_sender.bot_manager = bot_manager

    assert (
        asyncio.run(dispatcher.dispatch("source-group", {}, "source-platform")) is True
    )
    dispatcher.feishu_sender.send_summary.assert_not_awaited()
    bot_manager.get_adapter.assert_any_call("lark-main")
    adapter = bot_manager.get_adapter.return_value
    adapter.send_private_text_report.assert_awaited_once()
    reuse_target, content = adapter.send_private_text_report.await_args.args
    assert reuse_target.user_id == "ou_abc"
    assert content.startswith("📊 每日群聊分析报告\n来源群：source-group\n\n")


def test_feishu_reuse_invalid_instance_does_not_undo_other_channels(tmp_path):
    dispatcher, source, target = make_dispatcher(tmp_path)
    dispatcher.config_manager.config["report_delivery"].update(
        destination="independent",
        forward_group_enabled=True,
        feishu_enabled=True,
        feishu_target="lark-main:FriendMessage:ou_abc",
    )
    target.send_text_report.return_value = True
    dispatcher.feishu_sender.send_summary = AsyncMock(return_value=True)
    bot_manager = Mock()
    bot_manager.validate_lark_report_target.side_effect = ValueError(
        "飞书目标平台未连接"
    )
    bot_manager.get_adapter = Mock(
        side_effect=lambda platform_id: {"target-platform": target}.get(platform_id)
    )
    bot_manager.get_adapter_platform_id = Mock(
        side_effect=lambda adapter: getattr(adapter, "platform_id", "")
    )
    dispatcher.message_sender.bot_manager = bot_manager

    assert (
        asyncio.run(dispatcher.dispatch("source-group", {}, "source-platform")) is True
    )
    target.send_text_report.assert_awaited_once()
    dispatcher.feishu_sender.send_summary.assert_not_awaited()


def test_lark_adapter_sends_documented_session_messages(tmp_path):
    from src.infrastructure.platform.adapters.lark_adapter import LarkAdapter

    adapter = LarkAdapter(object(), {"platform_id": "lark-main"})
    context = SimpleNamespace(send_message=AsyncMock(return_value=True))
    adapter.set_context(context)

    assert asyncio.run(adapter.send_text("oc_xyz", "群总结")) is True
    assert asyncio.run(
        adapter.send_private_text(FeishuReuseTarget("ou_abc", "lark-main"), "私聊")
    ) is True
    sent_umos = [call.args[0] for call in context.send_message.await_args_list]
    assert sent_umos == ["lark-main:GroupMessage:oc_xyz", "lark-main:FriendMessage:ou_abc"]
    assert context.send_message.await_args_list[0].args[1] is not None


def test_lark_adapter_requires_context():
    from src.infrastructure.platform.adapters.lark_adapter import LarkAdapter

    adapter = LarkAdapter(object(), {"platform_id": "lark-main"})
    assert (
        asyncio.run(adapter.send_text("oc_xyz", "群总结")) is False
    )


def test_invalid_feishu_config_does_not_undo_successful_group_delivery(
    tmp_path,
):
    dispatcher, source, target = make_dispatcher(tmp_path)
    settings = dispatcher.config_manager.config["report_delivery"]
    settings.update(
        destination="independent", forward_group_enabled=True, feishu_enabled=True
    )
    settings["feishu_app_id"] = ""
    target.send_text_report.return_value = True
    dispatcher.feishu_sender.send_summary = AsyncMock()

    assert (
        asyncio.run(dispatcher.dispatch("source-group", {}, "source-platform")) is True
    )
    target.send_text_report.assert_awaited_once_with(
        "target-group", "来源群：source-group\n\n源群总结正文"
    )
    dispatcher.feishu_sender.send_summary.assert_not_awaited()
    source.send_text_report.assert_not_awaited()


def test_dual_bot_channels_attempt_both_and_record_partial_results(tmp_path):
    dispatcher, source, target = make_dispatcher(tmp_path)
    dispatcher.config_manager.config["report_delivery"].update(
        destination="independent", serverchan_enabled=True, feishu_enabled=True
    )
    dispatcher.serverchan_sender.send_summary = AsyncMock(return_value=True)
    dispatcher.feishu_sender.send_summary = AsyncMock(return_value=False)
    trace = TraceContext(group_id="source-group", platform="source-platform")
    token = _current_trace.set(trace)
    try:
        sent = asyncio.run(dispatcher.dispatch("source-group", {}, "source-platform"))
    finally:
        _current_trace.reset(token)

    assert sent is True
    dispatcher.serverchan_sender.send_summary.assert_awaited_once()
    dispatcher.feishu_sender.send_summary.assert_awaited_once()
    assert trace.metadata["report_delivery_results"] == {
        "serverchan": True,
        "feishu": False,
    }
    source.send_text_report.assert_not_awaited()


def test_feishu_delivery_prepends_source_label(tmp_path):
    dispatcher, source, target = make_dispatcher(tmp_path)
    dispatcher.config_manager.config["report_delivery"].update(
        destination="independent", feishu_enabled=True
    )
    dispatcher.feishu_sender.send_summary = AsyncMock(return_value=True)

    assert (
        asyncio.run(dispatcher.dispatch("source-group", {}, "source-platform")) is True
    )
    text = dispatcher.feishu_sender.send_summary.await_args.args[1]
    assert text.startswith("📊 每日群聊分析报告\n来源群：source-group\n\n")
    assert text.endswith("源群总结正文")
    source.send_text_report.assert_not_awaited()
    target.send_text_report.assert_not_awaited()


# ================================================================
# 飞书机器人漫画：图片上传、富文本消息与失败处理
# ================================================================


def stub_feishu_comic_http(
    monkeypatch,
    *,
    upload_body=_FEISHU_DEFAULT_UPLOAD_BODY,
    status=200,
    body=_FEISHU_DEFAULT_SEND_BODY,
    download_status=200,
    download_body=b"remote-image",
    send_error=None,
):
    """按顺序返回访问凭证、图片上传与消息发送三个响应的 HTTP 桩。"""
    token_response = MagicMock()
    token_response.status = 200
    token_response.__aenter__ = AsyncMock(return_value=token_response)
    token_response.__aexit__ = AsyncMock(return_value=False)
    token_response.json = AsyncMock(
        return_value={"code": 0, "tenant_access_token": "t-token"}
    )
    upload_response = MagicMock()
    upload_response.status = 200
    upload_response.__aenter__ = AsyncMock(return_value=upload_response)
    upload_response.__aexit__ = AsyncMock(return_value=False)
    upload_response.json = AsyncMock(return_value=upload_body)
    send_response = MagicMock()
    send_response.status = status
    send_response.__aenter__ = AsyncMock(return_value=send_response)
    send_response.__aexit__ = AsyncMock(return_value=False)
    send_response.json = AsyncMock(return_value=body)
    download_response = MagicMock()
    download_response.status = download_status
    download_response.__aenter__ = AsyncMock(return_value=download_response)
    download_response.__aexit__ = AsyncMock(return_value=False)
    download_response.read = AsyncMock(return_value=download_body)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    if send_error is not None:
        session.post.side_effect = send_error
    else:
        session.post.side_effect = [token_response, upload_response, send_response]
    session.get = Mock(return_value=download_response)
    factory = Mock(return_value=session)
    monkeypatch.setattr(feishu_bot_sender.aiohttp, "ClientSession", factory)
    return factory, session, upload_response, send_response


def test_feishu_comic_uploads_local_image_and_sends_post_message(
    monkeypatch, tmp_path
):
    comic_file = tmp_path / "comic.png"
    comic_file.write_bytes(b"\x89PNG comic")
    _, session, upload_response, _ = stub_feishu_comic_http(monkeypatch)
    assert (
        asyncio.run(
            FeishuBotSender().send_comic(make_feishu_config(), str(comic_file), "来源群：A")
        )
        is True
    )
    upload_call, send_call = session.post.call_args_list[1:]
    assert upload_call.args[0] == "https://open.feishu.cn/open-apis/im/v1/images"
    assert upload_call.kwargs["headers"] == {"Authorization": "Bearer t-token"}
    assert upload_response.json.await_count == 1
    assert send_call.args[0] == "https://open.feishu.cn/open-apis/im/v1/messages"
    assert send_call.kwargs["params"] == {"receive_id_type": "open_id"}
    assert send_call.kwargs["headers"] == {"Authorization": "Bearer t-token"}
    assert send_call.kwargs["json"]["receive_id"] == "ou_abc"
    assert send_call.kwargs["json"]["msg_type"] == "post"
    content = json.loads(send_call.kwargs["json"]["content"])
    zh_cn = content["post"]["zh_cn"]
    assert zh_cn["title"] == "📊 每日群聊漫画"
    assert zh_cn["content"][0][0]["text"] == "来源群：A\n"
    assert zh_cn["content"][1][0]["image_key"] == "img_v2_xxx"


def test_feishu_comic_downloads_remote_image_before_upload(monkeypatch):
    _, session, upload_response, _ = stub_feishu_comic_http(monkeypatch)
    assert (
        asyncio.run(
            FeishuBotSender().send_comic(
                make_feishu_config(), "https://example.com/comic.png", ""
            )
        )
        is True
    )
    assert session.get.call_args.args[0] == "https://example.com/comic.png"
    upload_response.json.assert_awaited_once()


@pytest.mark.parametrize(
    ("download_status", "download_body"),
    [(404, b""), (200, b"")],
)
def test_feishu_comic_rejects_failed_or_empty_downloads(
    monkeypatch, download_status, download_body
):
    _, session, _, _ = stub_feishu_comic_http(
        monkeypatch, download_status=download_status, download_body=download_body
    )
    assert (
        asyncio.run(
            FeishuBotSender().send_comic(
                make_feishu_config(), "https://example.com/comic.png", ""
            )
        )
        is False
    )
    assert session.post.call_count == 0


def test_feishu_comic_rejects_oversized_local_image(monkeypatch, tmp_path):
    factory, session, _, _ = stub_feishu_comic_http(monkeypatch)
    comic_file = tmp_path / "comic.png"
    comic_file.write_bytes(b"x" * (10 * 1024 * 1024 + 1))
    assert (
        asyncio.run(
            FeishuBotSender().send_comic(make_feishu_config(), str(comic_file), "")
        )
        is False
    )
    factory.assert_not_called()


def test_feishu_comic_local_image_read_failure_does_not_request(monkeypatch, tmp_path):
    factory, _, _, _ = stub_feishu_comic_http(monkeypatch)
    assert (
        asyncio.run(
            FeishuBotSender().send_comic(
                make_feishu_config(), str(tmp_path / "missing.png"), ""
            )
        )
        is False
    )
    factory.assert_not_called()


@pytest.mark.parametrize(
    "upload_body",
    [
        {"code": 230001, "msg": "无 im:resource 权限"},
        {"code": 0},
        {"code": 0, "data": {}},
        None,
        [],
    ],
)
def test_feishu_comic_does_not_send_message_when_upload_fails(
    monkeypatch, tmp_path, upload_body
):
    comic_file = tmp_path / "comic.png"
    comic_file.write_bytes(b"\x89PNG comic")
    _, session, _, _ = stub_feishu_comic_http(monkeypatch, upload_body=upload_body)
    assert (
        asyncio.run(
            FeishuBotSender().send_comic(make_feishu_config(), str(comic_file), "")
        )
        is False
    )
    assert session.post.call_count == 2


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (403, {"code": 0, "msg": "success", "data": {"message_id": "om_xxx"}}),
        (200, {"code": 230013, "msg": "receive_id 无效"}),
        (200, {"code": 0}),
    ],
)
def test_feishu_comic_does_not_treat_rejected_send_as_success(
    monkeypatch, tmp_path, status, body
):
    comic_file = tmp_path / "comic.png"
    comic_file.write_bytes(b"\x89PNG comic")
    _, _, _, _ = stub_feishu_comic_http(monkeypatch, status=status, body=body)
    assert (
        asyncio.run(
            FeishuBotSender().send_comic(make_feishu_config(), str(comic_file), "")
        )
        is False
    )


def test_feishu_comic_errors_do_not_log_credentials(monkeypatch, tmp_path):
    comic_file = tmp_path / "comic.png"
    comic_file.write_bytes(b"\x89PNG comic")
    stub_feishu_comic_http(
        monkeypatch, send_error=[ValueError("boom secret-value"), Mock(), Mock()]
    )
    log = Mock()
    monkeypatch.setattr(feishu_bot_sender, "logger", log)
    assert (
        asyncio.run(
            FeishuBotSender().send_comic(make_feishu_config(), str(comic_file), "")
        )
        is False
    )
    log.error.assert_called_once_with("[飞书机器人] 漫画推送异常：ValueError")
    assert "secret-value" not in repr(log.mock_calls)
