"""QQ 官方私聊适配器复用宿主接口及禁止群聊回退的回归。"""

import asyncio
import base64
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.domain.value_objects.report_delivery import QQOfficialPrivateTarget
from src.infrastructure.platform.adapters.qq_official_adapter import QQOfficialAdapter


class FakeMessageChain:
    """在宿主存根边界记录消息链内容，不替换被测适配器。"""

    def __init__(self, chain=None):
        self.parts = list(chain or [])

    def message(self, text):
        self.parts.append(("text", text))
        return self

    def url_image(self, url):
        self.parts.append(("url_image", url))
        return self

    def file_image(self, path):
        self.parts.append(("file_image", path))
        return self

    def base64_image(self, data):
        self.parts.append(("base64_image", data))
        return self


@pytest.fixture
def private_adapter_rig(monkeypatch):
    """隔离外部网络，直接运行真实适配器及其场景选择逻辑。"""
    import astrbot.api.event as event_module

    monkeypatch.setattr(event_module, "MessageChain", FakeMessageChain, raising=False)
    parser = AsyncMock(return_value=("总结正文", None, None, None, None, None, None))
    module_name = "astrbot.core.platform.sources.qqofficial.qqofficial_message_event"
    event = ModuleType(module_name)
    event.QQOfficialMessageEvent = SimpleNamespace(_parse_to_qqofficial=parser)
    monkeypatch.setitem(sys.modules, module_name, event)
    api = SimpleNamespace(
        create_dms=AsyncMock(return_value={"guild_id": "private-session"}),
        post_dms=AsyncMock(return_value={"id": "sent-message"}),
        post_group_message=AsyncMock(),
    )
    platform = SimpleNamespace(config={"appid": "123"}, remember_session_scene=Mock())
    adapter = QQOfficialAdapter(
        SimpleNamespace(api=api, platform=platform), {"platform_id": "official-main"}
    )
    context = SimpleNamespace(send_message=AsyncMock(return_value=True))
    adapter.set_context(context)
    adapter._send_chain = AsyncMock(return_value=False)
    return SimpleNamespace(
        adapter=adapter,
        api=api,
        context=context,
        platform=platform,
        parser=parser,
        c2c=QQOfficialPrivateTarget("user-openid", "official-main"),
        guild=QQOfficialPrivateTarget(
            "guild-user", "official-main", "guild", "source-guild"
        ),
    )


def test_c2c_uses_friend_session_and_restores_host_scene(private_adapter_rig):
    rig = private_adapter_rig
    assert asyncio.run(rig.adapter.send_private_text(rig.c2c, "总结正文")) is True
    rig.platform.remember_session_scene.assert_called_once_with("user-openid", "friend")
    rig.context.send_message.assert_awaited_once()
    session, chain = rig.context.send_message.await_args.args
    assert session == "official-main:FriendMessage:user-openid"
    assert chain.parts == [("text", "总结正文")]
    rig.parser.assert_not_awaited()
    rig.api.create_dms.assert_not_awaited()
    rig.api.post_group_message.assert_not_awaited()
    rig.adapter._send_chain.assert_not_awaited()


@pytest.mark.parametrize("failure", ["no_context", "rejected", "exception"])
def test_c2c_failure_never_switches_to_group_or_guild(private_adapter_rig, failure):
    rig = private_adapter_rig
    if failure == "no_context":
        rig.adapter = QQOfficialAdapter(
            rig.adapter.bot, {"platform_id": "official-main"}
        )
        rig.adapter._send_chain = AsyncMock(return_value=False)
    elif failure == "rejected":
        rig.context.send_message.return_value = False
    else:
        rig.context.send_message.side_effect = RuntimeError("模拟私聊拒绝")
    assert asyncio.run(rig.adapter.send_private_text(rig.c2c, "总结正文")) is False
    rig.api.create_dms.assert_not_awaited()
    rig.api.post_group_message.assert_not_awaited()
    rig.adapter._send_chain.assert_not_awaited()


@pytest.mark.parametrize("scene", ["c2c", "guild"])
def test_private_report_chunks_stop_after_failed_chunk(private_adapter_rig, scene):
    rig = private_adapter_rig
    target = getattr(rig, scene)
    rig.adapter.send_private_text = AsyncMock(side_effect=[True, False])
    assert (
        asyncio.run(rig.adapter.send_private_text_report(target, "正文" * 6000))
        is False
    )
    assert rig.adapter.send_private_text.await_count == 2
    assert all(
        call.args[0] == target for call in rig.adapter.send_private_text.await_args_list
    )
    assert all(
        len(call.args[1]) <= rig.adapter.MARKDOWN_CHUNK_SIZE
        for call in rig.adapter.send_private_text.await_args_list
    )
    rig.adapter._send_chain.assert_not_awaited()


def test_private_report_keeps_plain_fallback_and_empty_report_is_not_sent(
    private_adapter_rig,
):
    rig = private_adapter_rig
    rig.adapter.send_private_text = AsyncMock(return_value=True)
    assert (
        asyncio.run(
            rig.adapter.send_private_text_report(rig.c2c, "Markdown", "普通正文")
        )
        is True
    )
    rig.adapter.send_private_text.assert_awaited_once_with(rig.c2c, "普通正文")
    rig.adapter.send_private_text.reset_mock()
    assert asyncio.run(rig.adapter.send_private_text_report(rig.c2c, "  ")) is False
    rig.adapter.send_private_text.assert_not_awaited()


@pytest.mark.parametrize("media", ["url", "local", "base64", "data_url"])
def test_c2c_images_use_host_message_chain(private_adapter_rig, tmp_path, media):
    rig = private_adapter_rig
    image = {
        "url": "https://example.com/image.png",
        "local": str(tmp_path / "image.png"),
        "base64": "base64://aW1hZ2U=",
        "data_url": "data:image/png;base64,aW1hZ2U=",
    }[media]
    assert (
        asyncio.run(rig.adapter.send_private_image(rig.c2c, image, caption="标题"))
        is True
    )
    session, chain = rig.context.send_message.await_args.args
    assert session == "official-main:FriendMessage:user-openid"
    expected_kind = {"url": "url_image", "local": "file_image"}.get(
        media, "base64_image"
    )
    assert chain.parts[0] == ("text", "标题")
    assert chain.parts[1][0] == expected_kind
    if media in {"base64", "data_url"}:
        assert chain.parts[1][1] == "aW1hZ2U="
    rig.api.post_group_message.assert_not_awaited()


def test_c2c_file_uses_host_file_component(private_adapter_rig, tmp_path):
    rig = private_adapter_rig
    path = tmp_path / "report.html"
    path.write_text("报告", encoding="utf-8")
    assert (
        asyncio.run(rig.adapter.send_private_file(rig.c2c, str(path), "report.html"))
        is True
    )
    session, chain = rig.context.send_message.await_args.args
    assert session == "official-main:FriendMessage:user-openid"
    assert len(chain.parts) == 1
    assert chain.parts[0].name == "report.html"
    assert Path(chain.parts[0].file) == path
    rig.adapter._send_chain.assert_not_awaited()


@pytest.mark.parametrize(
    "response",
    [{"guild_id": " private-session "}, SimpleNamespace(guild_id="private-session")],
)
def test_guild_uses_created_private_session_instead_of_source_guild(
    private_adapter_rig, response
):
    rig = private_adapter_rig
    rig.api.create_dms.return_value = response
    assert asyncio.run(rig.adapter.send_private_text(rig.guild, "总结正文")) is True
    rig.parser.assert_awaited_once()
    rig.api.create_dms.assert_awaited_once_with(
        guild_id="source-guild", user_id="guild-user"
    )
    rig.api.post_dms.assert_awaited_once_with(
        guild_id="private-session", content="总结正文"
    )
    rig.context.send_message.assert_not_awaited()
    rig.platform.remember_session_scene.assert_not_called()
    rig.api.post_group_message.assert_not_awaited()
    rig.adapter._send_chain.assert_not_awaited()


@pytest.mark.parametrize(
    "response", [{}, {"guild_id": ""}, {"guild_id": " "}, {"guild_id": 123}, None]
)
def test_invalid_guild_session_does_not_send_anywhere(private_adapter_rig, response):
    rig = private_adapter_rig
    rig.api.create_dms.return_value = response
    assert asyncio.run(rig.adapter.send_private_text(rig.guild, "总结正文")) is False
    rig.api.post_dms.assert_not_awaited()
    rig.context.send_message.assert_not_awaited()
    rig.api.post_group_message.assert_not_awaited()
    rig.adapter._send_chain.assert_not_awaited()


@pytest.mark.parametrize(
    "failure",
    ["missing_api", "missing_source", "create_error", "send_error", "rejected"],
)
def test_guild_api_failures_do_not_fall_back_to_c2c_or_group(
    private_adapter_rig, failure
):
    rig = private_adapter_rig
    target = rig.guild
    if failure == "missing_api":
        rig.adapter.bot.api = SimpleNamespace()
    elif failure == "missing_source":
        target = QQOfficialPrivateTarget("guild-user", "official-main", "guild")
    elif failure == "create_error":
        rig.api.create_dms.side_effect = RuntimeError("模拟创建失败")
    elif failure == "send_error":
        rig.api.post_dms.side_effect = RuntimeError("模拟发送失败")
    else:
        rig.api.post_dms.return_value = None
    assert asyncio.run(rig.adapter.send_private_text(target, "总结正文")) is False
    rig.context.send_message.assert_not_awaited()
    rig.api.post_group_message.assert_not_awaited()
    rig.adapter._send_chain.assert_not_awaited()


@pytest.mark.parametrize("media", ["local", "base64"])
def test_guild_images_reuse_host_parser_and_bot_api(
    private_adapter_rig, tmp_path, media
):
    rig = private_adapter_rig
    path = str(tmp_path / "image.png")
    encoded = base64.b64encode(b"image-data").decode("ascii")
    rig.parser.return_value = (
        "标题",
        encoded if media == "base64" else None,
        path if media == "local" else None,
        None,
        None,
        None,
        None,
    )
    image = path if media == "local" else f"base64://{encoded}"
    assert asyncio.run(rig.adapter.send_private_image(rig.guild, image, "标题")) is True
    rig.api.post_dms.assert_awaited_once_with(
        guild_id="private-session",
        content="标题",
        file_image=path if media == "local" else b"image-data",
    )
    rig.context.send_message.assert_not_awaited()
    rig.api.post_group_message.assert_not_awaited()


def test_guild_file_is_rejected_before_api_for_private_text_fallback(
    private_adapter_rig,
):
    rig = private_adapter_rig
    assert (
        asyncio.run(
            rig.adapter.send_private_file(rig.guild, "report.html", "report.html")
        )
        is False
    )
    rig.api.create_dms.assert_not_awaited()
    rig.api.post_dms.assert_not_awaited()
    rig.context.send_message.assert_not_awaited()


def test_invalid_guild_image_does_not_send_or_switch_scene(private_adapter_rig):
    rig = private_adapter_rig
    rig.parser.return_value = ("", "invalid!", None, None, None, None, None)
    assert (
        asyncio.run(rig.adapter.send_private_image(rig.guild, "base64://invalid!"))
        is False
    )
    rig.api.post_dms.assert_not_awaited()
    rig.context.send_message.assert_not_awaited()
    rig.adapter._send_chain.assert_not_awaited()


@pytest.mark.parametrize("caption", ["", "来源群：讨论群（123456）"])
def test_c2c_attachment_and_source_caption_share_host_message_chain(
    private_adapter_rig, tmp_path, caption
):
    rig = private_adapter_rig
    path = tmp_path / "report.html"
    path.write_text("报告", encoding="utf-8")
    assert (
        asyncio.run(
            rig.adapter.send_private_file(
                rig.c2c, str(path), path.name, caption=caption
            )
        )
        is True
    )
    session, chain = rig.context.send_message.await_args.args
    assert session == "official-main:FriendMessage:user-openid"
    assert chain.parts[0].name == path.name
    assert chain.parts[0].file == str(path.resolve())
    assert chain.parts[1:] == ([("text", caption)] if caption else [])
    rig.api.create_dms.assert_not_awaited()
    rig.api.post_group_message.assert_not_awaited()
    rig.adapter._send_chain.assert_not_awaited()
