"""漫画唯一投递目标、实时配置和禁止回发原群的回归测试。"""

import asyncio
from itertools import product
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.application.handlers.comic_command_handler import ComicCommandHandler
from src.infrastructure.config.config_manager import ConfigManager
from src.infrastructure.platform.bot_manager import BotManager
from src.shared.trace_context import _current_trace

SOURCE_GROUP = "123456"
SOURCE_PLATFORM = "source-platform"
SOURCE_UMO = f"{SOURCE_PLATFORM}:GroupMessage:{SOURCE_GROUP}"
TARGET_GROUP = "654321"
TARGET_PLATFORM = "target-platform"
PNG_BYTES = b"\x89PNG\r\n\x1a\ncomic-payload"
IMAGE_URL = "https://example.com/comic.png"
TOPICS = [{"topic": "测试话题", "detail": "源群讨论详情"}]


@pytest.fixture
def comic_rig(tmp_path):
    """使用真实配置管理器和漫画处理器，隔离绘图及平台网络请求。"""
    config = object.__new__(ConfigManager)
    config.config = {
        "basic": {"show_report_caption": False},
        "report_delivery": {
            "destination": "forward_group",
            "forward_target": f"{TARGET_PLATFORM}:GroupMessage:{TARGET_GROUP}",
        },
        "daily_comic": {
            "enable_daily_comic": True,
            "enable_auto_daily_comic": True,
            "comic_group_list_mode": "whitelist",
            "comic_group_list": [SOURCE_UMO],
        },
        "qq_group_upload": {
            "enable_comic_album_upload": True,
            "comic_album_name": "comic",
            "group_album_strict_mode": False,
        },
    }
    source = SimpleNamespace(
        platform_id=SOURCE_PLATFORM,
        send_image=AsyncMock(return_value=True),
        upload_group_album=AsyncMock(return_value=True),
        get_group_info=AsyncMock(return_value=None),
    )
    target = SimpleNamespace(
        platform_id=TARGET_PLATFORM,
        send_image=AsyncMock(return_value=True),
        upload_group_album=AsyncMock(return_value=True),
    )
    adapters = {SOURCE_PLATFORM: source, TARGET_PLATFORM: target}
    bot_manager = SimpleNamespace(
        get_adapter=Mock(side_effect=adapters.get),
        get_adapter_platform_id=Mock(side_effect=lambda adapter: adapter.platform_id),
        update_from_event=Mock(),
    )
    comic_service = SimpleNamespace(
        generate_comic=AsyncMock(return_value=(PNG_BYTES, None))
    )
    analysis_service = SimpleNamespace(
        execute_comic_topic_analysis=AsyncMock(
            return_value={"success": True, "topics": TOPICS}
        )
    )
    handler = ComicCommandHandler(
        config_manager=config,
        bot_manager=bot_manager,
        comic_service=comic_service,
        analysis_service=analysis_service,
        plugin_data_dir=tmp_path,
    )
    token = _current_trace.set(None)
    yield SimpleNamespace(
        config=config,
        settings=config.config["report_delivery"],
        handler=handler,
        source=source,
        target=target,
        adapters=adapters,
        comic_service=comic_service,
        analysis_service=analysis_service,
        data_dir=tmp_path,
    )
    _current_trace.reset(token)


async def generate(rig):
    await rig.handler._trigger_comic_generation(
        TOPICS, SOURCE_GROUP, SOURCE_PLATFORM, SOURCE_UMO
    )


def set_output(rig, output):
    rig.comic_service.generate_comic.return_value = (
        (PNG_BYTES, None) if output == "bytes" else (None, IMAGE_URL)
    )


def make_event():
    return SimpleNamespace(
        get_group_id=Mock(return_value=SOURCE_GROUP),
        get_platform_id=Mock(return_value=SOURCE_PLATFORM),
        unified_msg_origin=SOURCE_UMO,
        should_call_llm=Mock(),
        plain_result=lambda text: text,
    )


@pytest.mark.parametrize("output", ["bytes", "url"])
@pytest.mark.parametrize("explicit_platform", [False, True])
@pytest.mark.asyncio
async def test_forward_comic_and_album_use_only_the_configured_target(
    comic_rig, output, explicit_platform
):
    rig = comic_rig
    set_output(rig, output)
    rig.settings["forward_target"] = (
        f"{TARGET_PLATFORM}:GroupMessage:{TARGET_GROUP}"
        if explicit_platform
        else TARGET_GROUP
    )
    adapter = rig.target if explicit_platform else rig.source

    await generate(rig)

    rig.comic_service.generate_comic.assert_awaited_once_with(
        TOPICS, SOURCE_GROUP, SOURCE_UMO
    )
    adapter.send_image.assert_awaited_once()
    sent_group, image = adapter.send_image.await_args.args
    assert sent_group == TARGET_GROUP
    if output == "bytes":
        image_path = Path(image)
        assert image_path.parent == rig.data_dir / "reports"
        assert image_path.name.startswith(f"comic_{SOURCE_GROUP}_")
        assert image_path.read_bytes() == PNG_BYTES
        adapter.upload_group_album.assert_awaited_once_with(
            TARGET_GROUP,
            str(image_path.resolve()),
            album_id=None,
            album_name="comic",
            strict_mode=False,
        )
    else:
        assert image == IMAGE_URL
        adapter.upload_group_album.assert_not_awaited()
    if explicit_platform:
        rig.source.send_image.assert_not_awaited()
        rig.source.upload_group_album.assert_not_awaited()
    assert all(
        call.args[0] != SOURCE_GROUP for call in adapter.send_image.await_args_list
    )


@pytest.mark.parametrize("output", ["bytes", "url"])
@pytest.mark.parametrize("old_config", [False, True])
@pytest.mark.asyncio
async def test_source_mode_and_old_config_keep_original_delivery(
    comic_rig, output, old_config
):
    rig = comic_rig
    set_output(rig, output)
    if old_config:
        rig.config.config.pop("report_delivery")
    else:
        rig.settings["destination"] = "source_group"

    await generate(rig)

    rig.source.send_image.assert_awaited_once()
    assert rig.source.send_image.await_args.args[0] == SOURCE_GROUP
    assert rig.source.upload_group_album.await_count == (1 if output == "bytes" else 0)
    if output == "bytes":
        assert rig.source.upload_group_album.await_args.args[0] == SOURCE_GROUP
    rig.target.send_image.assert_not_awaited()
    rig.target.upload_group_album.assert_not_awaited()


@pytest.mark.parametrize("require_auto", [False, True])
@pytest.mark.parametrize(
    ("settings", "status"),
    [
        ({"destination": "serverchan"}, "delivery_disabled"),
        (
            {"destination": "forward_group", "forward_target": ""},
            "delivery_unavailable",
        ),
        (
            {"destination": "forward_group", "forward_target": SOURCE_GROUP},
            "delivery_unavailable",
        ),
        ({"destination": "invalid"}, "delivery_unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_or_serverchan_destination_never_creates_comic_task(
    comic_rig, settings, status, require_auto
):
    rig = comic_rig
    rig.settings.update(settings)

    assert (
        rig.handler.try_trigger_comic_generation(
            SOURCE_GROUP,
            SOURCE_PLATFORM,
            {"topics": TOPICS},
            require_auto_enabled=require_auto,
        )
        == status
    )

    assert rig.handler._background_tasks == set()
    assert rig.handler._comic_group_tasks == {}
    rig.comic_service.generate_comic.assert_not_awaited()
    rig.source.send_image.assert_not_awaited()
    rig.target.send_image.assert_not_awaited()


@pytest.mark.parametrize("same_object", [False, True])
@pytest.mark.asyncio
async def test_forward_alias_of_source_group_is_blocked(comic_rig, same_object):
    rig = comic_rig
    alias = (
        rig.source
        if same_object
        else SimpleNamespace(platform_id=SOURCE_PLATFORM.upper())
    )
    rig.adapters["alias"] = alias
    rig.settings["forward_target"] = f"alias:GroupMessage:{SOURCE_GROUP}"

    assert (
        rig.handler.try_trigger_comic_generation(
            SOURCE_GROUP, SOURCE_PLATFORM, {"topics": TOPICS}
        )
        == "delivery_unavailable"
    )
    rig.comic_service.generate_comic.assert_not_awaited()
    assert not rig.handler._background_tasks


@pytest.mark.asyncio
async def test_same_group_number_on_different_platform_is_allowed(comic_rig):
    rig = comic_rig
    rig.settings["forward_target"] = f"{TARGET_PLATFORM}:GroupMessage:{SOURCE_GROUP}"

    await generate(rig)

    assert rig.target.send_image.await_args.args[0] == SOURCE_GROUP
    assert rig.target.upload_group_album.await_args.args[0] == SOURCE_GROUP
    rig.source.send_image.assert_not_awaited()
    rig.source.upload_group_album.assert_not_awaited()


@pytest.mark.parametrize("output", ["bytes", "url"])
@pytest.mark.parametrize("failure", ["missing_adapter", "rejected", "exception"])
@pytest.mark.asyncio
async def test_forward_failure_never_falls_back_or_uploads_to_source(
    comic_rig, output, failure
):
    rig = comic_rig
    set_output(rig, output)
    if failure == "missing_adapter":
        rig.adapters.pop(TARGET_PLATFORM)
    elif failure == "rejected":
        rig.target.send_image.return_value = False
    else:
        rig.target.send_image.side_effect = RuntimeError("目标平台发送失败")

    await generate(rig)

    assert rig.target.send_image.await_count == (
        0 if failure == "missing_adapter" else 1
    )
    rig.target.upload_group_album.assert_not_awaited()
    rig.source.send_image.assert_not_awaited()
    rig.source.upload_group_album.assert_not_awaited()


@pytest.mark.parametrize(
    "settings",
    [
        {"destination": "serverchan"},
        {"destination": "forward_group", "forward_target": ""},
    ],
)
@pytest.mark.asyncio
async def test_queued_comic_rechecks_destination_before_generation(comic_rig, settings):
    rig = comic_rig
    await rig.handler._comic_semaphore.acquire()
    assert (
        rig.handler.try_trigger_comic_generation(
            SOURCE_GROUP, SOURCE_PLATFORM, {"topics": TOPICS}
        )
        == "started"
    )
    tasks = tuple(rig.handler._background_tasks)
    await asyncio.sleep(0)
    rig.comic_service.generate_comic.assert_not_awaited()
    rig.settings.update(settings)
    rig.handler._comic_semaphore.release()
    await asyncio.gather(*tasks)

    rig.comic_service.generate_comic.assert_not_awaited()
    rig.target.send_image.assert_not_awaited()
    rig.source.send_image.assert_not_awaited()
    assert not rig.handler._comic_group_tasks


@pytest.mark.parametrize("output", ["bytes", "url"])
@pytest.mark.parametrize("change", ["serverchan", "invalid", "new_group"])
@pytest.mark.asyncio
async def test_destination_changed_during_drawing_is_rechecked(
    comic_rig, output, change
):
    rig = comic_rig

    async def draw(*_args):
        if change == "serverchan":
            rig.settings["destination"] = "serverchan"
        else:
            rig.settings["forward_target"] = (
                "" if change == "invalid" else f"{TARGET_PLATFORM}:GroupMessage:777777"
            )
        return (PNG_BYTES, None) if output == "bytes" else (None, IMAGE_URL)

    rig.comic_service.generate_comic.side_effect = draw
    await generate(rig)

    rig.comic_service.generate_comic.assert_awaited_once_with(
        TOPICS, SOURCE_GROUP, SOURCE_UMO
    )
    if change == "new_group":
        assert rig.target.send_image.await_args.args[0] == "777777"
        if output == "bytes":
            assert rig.target.upload_group_album.await_args.args[0] == "777777"
    else:
        rig.target.send_image.assert_not_awaited()
        rig.target.upload_group_album.assert_not_awaited()
    rig.source.send_image.assert_not_awaited()
    rig.source.upload_group_album.assert_not_awaited()


@pytest.mark.parametrize("change", ["serverchan", "invalid", "new_group"])
@pytest.mark.asyncio
async def test_configuration_changed_during_send_skips_album_backup(comic_rig, change):
    rig = comic_rig

    async def send(*_args, **_kwargs):
        if change == "serverchan":
            rig.settings["destination"] = "serverchan"
        else:
            rig.settings["forward_target"] = (
                "" if change == "invalid" else f"{TARGET_PLATFORM}:GroupMessage:777777"
            )
        return True

    rig.target.send_image.side_effect = send
    await generate(rig)

    rig.target.send_image.assert_awaited_once()
    assert rig.target.send_image.await_args.args[0] == TARGET_GROUP
    rig.target.upload_group_album.assert_not_awaited()
    rig.source.send_image.assert_not_awaited()
    rig.source.upload_group_album.assert_not_awaited()


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        ({"destination": "serverchan"}, "未启用群目标"),
        ({"destination": "forward_group", "forward_target": ""}, "投递目标无效"),
    ],
)
@pytest.mark.asyncio
async def test_manual_comic_rejects_unusable_destination_before_topic_analysis(
    comic_rig, settings, expected
):
    rig = comic_rig
    rig.settings.update(settings)
    messages = [
        message async for message in rig.handler.handle_group_comic(make_event())
    ]

    assert any(expected in message for message in messages)
    rig.analysis_service.execute_comic_topic_analysis.assert_not_awaited()
    rig.comic_service.generate_comic.assert_not_awaited()
    rig.source.get_group_info.assert_not_awaited()
    rig.source.send_image.assert_not_awaited()
    rig.target.send_image.assert_not_awaited()
    assert not rig.handler._background_tasks


@pytest.mark.parametrize("change", ["serverchan", "invalid"])
@pytest.mark.asyncio
async def test_manual_comic_handles_config_change_during_topic_analysis(
    comic_rig, change
):
    rig = comic_rig
    manager = SimpleNamespace(register_task=AsyncMock(), finish_task=AsyncMock())
    rig.handler.active_task_manager = manager

    async def analyze(**_kwargs):
        if change == "serverchan":
            rig.settings["destination"] = "serverchan"
        else:
            rig.settings["forward_target"] = ""
        return {"success": True, "topics": TOPICS}

    rig.analysis_service.execute_comic_topic_analysis.side_effect = analyze
    messages = [
        message async for message in rig.handler.handle_group_comic(make_event())
    ]

    assert any(
        "未发送到原群" in message or "不会发送到原群" in message for message in messages
    )
    manager.finish_task.assert_awaited_once()
    rig.comic_service.generate_comic.assert_not_awaited()
    rig.source.send_image.assert_not_awaited()
    rig.target.send_image.assert_not_awaited()
    assert not rig.handler._background_tasks


@pytest.mark.asyncio
async def test_manual_comic_uses_target_even_when_auto_generation_is_disabled(
    comic_rig,
):
    rig = comic_rig
    rig.config.config["daily_comic"]["enable_auto_daily_comic"] = False
    messages = [
        message async for message in rig.handler.handle_group_comic(make_event())
    ]
    tasks = tuple(rig.handler._background_tasks)
    assert any("完成后会发送到配置的目标群" in message for message in messages)
    await asyncio.gather(*tasks)

    rig.analysis_service.execute_comic_topic_analysis.assert_awaited_once_with(
        group_id=SOURCE_GROUP, platform_id=SOURCE_PLATFORM, days=None
    )
    assert rig.target.send_image.await_args.args[0] == TARGET_GROUP
    rig.source.send_image.assert_not_awaited()
    rig.source.upload_group_album.assert_not_awaited()


@pytest.mark.parametrize("setting", ["disabled", "auto_disabled", "blocked"])
@pytest.mark.asyncio
async def test_comic_switches_and_source_group_permissions_still_apply(
    comic_rig, setting
):
    rig = comic_rig
    comic_settings = rig.config.config["daily_comic"]
    if setting == "disabled":
        comic_settings["enable_daily_comic"] = False
    elif setting == "auto_disabled":
        comic_settings["enable_auto_daily_comic"] = False
    else:
        comic_settings["comic_group_list"] = [
            f"{TARGET_PLATFORM}:GroupMessage:{TARGET_GROUP}"
        ]

    assert (
        rig.handler.try_trigger_comic_generation(
            SOURCE_GROUP, SOURCE_PLATFORM, {"topics": TOPICS}
        )
        == setting
    )
    rig.comic_service.generate_comic.assert_not_awaited()
    assert not rig.handler._background_tasks


@pytest.mark.asyncio
async def test_comic_deduplication_remains_keyed_by_source_group(comic_rig):
    rig = comic_rig
    assert (
        rig.handler.try_trigger_comic_generation(
            SOURCE_GROUP, SOURCE_PLATFORM, {"topics": TOPICS}
        )
        == "started"
    )
    assert set(rig.handler._comic_group_tasks) == {f"{SOURCE_PLATFORM}:{SOURCE_GROUP}"}
    assert (
        rig.handler.try_trigger_comic_generation(
            SOURCE_GROUP, SOURCE_PLATFORM, {"topics": TOPICS}
        )
        == "duplicate"
    )
    await asyncio.gather(*tuple(rig.handler._background_tasks))

    rig.comic_service.generate_comic.assert_awaited_once()
    rig.target.send_image.assert_awaited_once()
    rig.source.send_image.assert_not_awaited()
    assert not rig.handler._comic_group_tasks


@pytest.mark.parametrize("forward", [False, True])
@pytest.mark.parametrize("serverchan", [False, True])
@pytest.mark.parametrize("require_auto", [False, True])
@pytest.mark.asyncio
async def test_comic_independent_switches_keep_the_correct_group_destination(
    comic_rig, forward, serverchan, require_auto
):
    rig = comic_rig
    rig.settings.update(
        destination="independent",
        forward_group_enabled=forward,
        serverchan_enabled=serverchan,
    )
    status = rig.handler.try_trigger_comic_generation(
        SOURCE_GROUP,
        SOURCE_PLATFORM,
        {"topics": TOPICS},
        require_auto_enabled=require_auto,
    )
    if serverchan and not forward:
        assert status == "delivery_disabled"
        rig.comic_service.generate_comic.assert_not_awaited()
        rig.source.send_image.assert_not_awaited()
        rig.target.send_image.assert_not_awaited()
        assert not rig.handler._background_tasks
        return

    assert status == "started"
    await asyncio.gather(*tuple(rig.handler._background_tasks))
    adapter = rig.target if forward else rig.source
    expected_group = TARGET_GROUP if forward else SOURCE_GROUP
    adapter.send_image.assert_awaited_once()
    assert adapter.send_image.await_args.args[0] == expected_group
    adapter.upload_group_album.assert_awaited_once()
    assert adapter.upload_group_album.await_args.args[0] == expected_group
    other_adapter = rig.source if forward else rig.target
    other_adapter.send_image.assert_not_awaited()
    other_adapter.upload_group_album.assert_not_awaited()
    rig.comic_service.generate_comic.assert_awaited_once_with(
        TOPICS, SOURCE_GROUP, SOURCE_UMO
    )


@pytest.mark.parametrize("disable", ["forward_group_enabled", "serverchan_enabled"])
@pytest.mark.asyncio
async def test_dual_delivery_comic_rechecks_group_switch_after_drawing(
    comic_rig, disable
):
    rig = comic_rig
    rig.settings.update(
        destination="independent", forward_group_enabled=True, serverchan_enabled=True
    )

    async def draw(*_args):
        rig.settings[disable] = False
        return PNG_BYTES, None

    rig.comic_service.generate_comic.side_effect = draw
    await generate(rig)

    if disable == "forward_group_enabled":
        rig.target.send_image.assert_not_awaited()
        rig.target.upload_group_album.assert_not_awaited()
    else:
        rig.target.send_image.assert_awaited_once()
        assert rig.target.send_image.await_args.args[0] == TARGET_GROUP
    rig.source.send_image.assert_not_awaited()
    rig.source.upload_group_album.assert_not_awaited()


@pytest.mark.parametrize("invalid_target", ["", SOURCE_GROUP])
@pytest.mark.asyncio
async def test_dual_delivery_does_not_return_comic_to_source_for_invalid_target(
    comic_rig, invalid_target
):
    rig = comic_rig
    rig.settings.update(
        destination="independent",
        forward_group_enabled=True,
        serverchan_enabled=True,
        forward_target=invalid_target,
    )
    assert (
        rig.handler.try_trigger_comic_generation(
            SOURCE_GROUP, SOURCE_PLATFORM, {"topics": TOPICS}
        )
        == "delivery_unavailable"
    )
    rig.comic_service.generate_comic.assert_not_awaited()
    rig.source.send_image.assert_not_awaited()
    rig.target.send_image.assert_not_awaited()


@pytest.fixture
def official_comic_rig(comic_rig):
    """为漫画回归接入真实实例选择器和不具备群相册能力的官方实例。"""
    rig = comic_rig
    official = SimpleNamespace(
        platform_id="official-main",
        get_platform_name=Mock(return_value="qq_official"),
        send_image=AsyncMock(return_value=True),
    )
    rig.source.get_platform_name = Mock(return_value="onebot")
    rig.target.get_platform_name = Mock(return_value="onebot")
    rig.adapters["official-main"] = official
    manager = BotManager(rig.config)
    manager._adapters = rig.adapters
    rig.handler.bot_manager = manager
    rig.official = official
    rig.settings.update(
        destination="independent",
        stop_source_group=False,
        forward_group_enabled=False,
        qq_official_enabled=False,
        serverchan_enabled=False,
        qq_official_target="official-main:GroupMessage:official-group",
    )
    return rig


@pytest.mark.parametrize(
    ("stop_source", "forward", "official", "serverchan"),
    list(product([False, True], repeat=4)),
)
@pytest.mark.parametrize("require_auto", [False, True])
@pytest.mark.asyncio
async def test_comic_three_channels_and_stop_source_use_one_destination(
    official_comic_rig, stop_source, forward, official, serverchan, require_auto
):
    rig = official_comic_rig
    rig.settings.update(
        stop_source_group=stop_source,
        forward_group_enabled=forward,
        qq_official_enabled=official,
        serverchan_enabled=serverchan,
    )
    destinations = [
        (rig.source, SOURCE_GROUP),
        (rig.target, TARGET_GROUP),
        (rig.official, "official-group"),
    ]
    winner = (
        rig.target
        if forward
        else rig.official
        if official
        else None
        if stop_source or serverchan
        else rig.source
    )
    status = rig.handler.try_trigger_comic_generation(
        SOURCE_GROUP,
        SOURCE_PLATFORM,
        {"topics": TOPICS},
        require_auto_enabled=require_auto,
    )
    if winner is None:
        assert status == "delivery_disabled"
        assert not rig.handler._background_tasks
        rig.comic_service.generate_comic.assert_not_awaited()
    else:
        assert status == "started"
        await asyncio.gather(*tuple(rig.handler._background_tasks))
        rig.comic_service.generate_comic.assert_awaited_once_with(
            TOPICS, SOURCE_GROUP, SOURCE_UMO
        )
    for adapter, group in destinations:
        if adapter is winner:
            adapter.send_image.assert_awaited_once()
            assert adapter.send_image.await_args.args[0] == group
            artifact = Path(adapter.send_image.await_args.args[1])
            assert artifact.is_file()
            assert artifact.name.startswith(f"comic_{SOURCE_GROUP}_")
        else:
            adapter.send_image.assert_not_awaited()
    for adapter, group in destinations[:2]:
        if adapter is winner:
            adapter.upload_group_album.assert_awaited_once()
            assert adapter.upload_group_album.await_args.args[0] == group
        else:
            adapter.upload_group_album.assert_not_awaited()


@pytest.mark.parametrize(
    "target",
    [
        "",
        "missing:GroupMessage:official-group",
        f"{TARGET_PLATFORM}:GroupMessage:official-group",
        "qq_official:GroupMessage:official-group",
    ],
)
@pytest.mark.asyncio
async def test_unavailable_official_comic_never_returns_to_source(
    official_comic_rig, target
):
    rig = official_comic_rig
    rig.settings.update(qq_official_enabled=True, qq_official_target=target)
    assert (
        rig.handler.try_trigger_comic_generation(
            SOURCE_GROUP, SOURCE_PLATFORM, {"topics": TOPICS}
        )
        == "delivery_unavailable"
    )
    rig.comic_service.generate_comic.assert_not_awaited()
    rig.source.send_image.assert_not_awaited()
    rig.target.send_image.assert_not_awaited()
    rig.official.send_image.assert_not_awaited()


@pytest.mark.parametrize("failure", ["rejected", "exception"])
@pytest.mark.asyncio
async def test_official_comic_send_failure_never_returns_to_source(
    official_comic_rig, failure
):
    rig = official_comic_rig
    rig.settings["qq_official_enabled"] = True
    rig.official.send_image.return_value = False
    if failure == "exception":
        rig.official.send_image.side_effect = RuntimeError("平台拒绝发送")
    await generate(rig)
    rig.official.send_image.assert_awaited_once()
    rig.source.send_image.assert_not_awaited()
    rig.source.upload_group_album.assert_not_awaited()
    rig.target.send_image.assert_not_awaited()
    rig.target.upload_group_album.assert_not_awaited()


@pytest.mark.parametrize("official", [False, True])
@pytest.mark.parametrize("phase", ["queued", "drawing"])
@pytest.mark.asyncio
async def test_stop_source_cancels_comic_before_generation_or_sending(
    official_comic_rig, official, phase
):
    rig = official_comic_rig
    rig.settings["qq_official_enabled"] = official

    def disable_delivery():
        rig.settings.update(stop_source_group=True, qq_official_enabled=False)

    if phase == "queued":
        await rig.handler._comic_semaphore.acquire()
        try:
            assert (
                rig.handler.try_trigger_comic_generation(
                    SOURCE_GROUP, SOURCE_PLATFORM, {"topics": TOPICS}
                )
                == "started"
            )
            disable_delivery()
        finally:
            rig.handler._comic_semaphore.release()
        await asyncio.gather(*tuple(rig.handler._background_tasks))
        rig.comic_service.generate_comic.assert_not_awaited()
    else:

        async def draw(*_args):
            disable_delivery()
            return PNG_BYTES, None

        rig.comic_service.generate_comic.side_effect = draw
        await generate(rig)
        rig.comic_service.generate_comic.assert_awaited_once()
    rig.source.send_image.assert_not_awaited()
    rig.target.send_image.assert_not_awaited()
    rig.official.send_image.assert_not_awaited()
    rig.source.upload_group_album.assert_not_awaited()
    rig.target.upload_group_album.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_source_skips_manual_comic_before_topic_analysis(comic_rig):
    rig = comic_rig
    rig.settings.update(
        destination="independent",
        stop_source_group=True,
        forward_group_enabled=False,
        qq_official_enabled=False,
        serverchan_enabled=False,
    )
    messages = [
        message async for message in rig.handler.handle_group_comic(make_event())
    ]
    assert any("未启用群目标" in message for message in messages)
    rig.analysis_service.execute_comic_topic_analysis.assert_not_awaited()
    rig.comic_service.generate_comic.assert_not_awaited()
    rig.source.send_image.assert_not_awaited()
    rig.target.send_image.assert_not_awaited()
    assert not rig.handler._background_tasks
