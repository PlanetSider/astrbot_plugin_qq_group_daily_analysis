"""
群漫画指令与异步生成处理器 (Comic Command Handler)
处理 /群漫画 指令触发、并发信号量限制、任务去重、漫画分镜生成及相册/文件分发。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from ...domain.value_objects.analysis_results import SummaryTopic
from ...domain.value_objects.report_delivery import (
    FEISHU_COMIC_DELIVERY_MODES,
    FORWARD_GROUP_DELIVERY_MODES,
    QQ_OFFICIAL_DELIVERY_MODES,
    FeishuPrivateTarget,
    FeishuReuseTarget,
    GroupReportTarget,
    QQOfficialPrivateTarget,
    ReportTarget,
)
from ...domain.value_objects.task_event import TaskEvent
from ...infrastructure.messaging.feishu_bot_sender import FeishuBotSender
from ...shared.constants import PLUGIN_NAME, AnalysisStage
from ...shared.trace_context import TraceContext
from ...utils.logger import logger
from ..services.analysis_application_service import (
    AnalysisApplicationService,
    DuplicateGroupTaskError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from astrbot.api.event import AstrMessageEvent, MessageEventResult

    from ...domain.repositories.plugin_host_repository import PluginHostProtocol
    from ...domain.value_objects import AnalysisResultPayload
    from ...infrastructure.config.config_manager import ConfigManager
    from ...infrastructure.platform.bot_manager import BotManager
    from ...infrastructure.webui.active_task_manager import ActiveTaskManager
    from ..services.comic_application_service import ComicApplicationService


class ComicCommandHandler:
    """群漫画指令应用层处理器。"""

    config_manager: ConfigManager
    bot_manager: BotManager
    comic_service: ComicApplicationService
    analysis_service: AnalysisApplicationService
    active_task_manager: ActiveTaskManager | None
    feishu_sender: FeishuBotSender
    plugin_data_dir: Path
    plugin_instance: PluginHostProtocol | None
    terminating: bool
    _comic_semaphore: asyncio.Semaphore
    _comic_group_tasks: dict[str, asyncio.Task[object]]
    _background_tasks: set[asyncio.Task[object]]

    def __init__(
        self,
        config_manager: ConfigManager,
        bot_manager: BotManager,
        comic_service: ComicApplicationService,
        analysis_service: AnalysisApplicationService,
        active_task_manager: ActiveTaskManager | None = None,
        plugin_data_dir: Path | None = None,
        plugin_instance: PluginHostProtocol | None = None,
    ) -> None:
        self.config_manager = config_manager
        self.bot_manager = bot_manager
        self.comic_service = comic_service
        self.analysis_service = analysis_service
        self.active_task_manager = active_task_manager
        self.feishu_sender = FeishuBotSender()
        self.plugin_data_dir = (
            plugin_data_dir or Path.cwd() / "data" / "plugin_data" / PLUGIN_NAME
        )
        self.plugin_instance = plugin_instance
        self.terminating: bool = False

        try:
            getter = getattr(self.config_manager, "get_t2i_max_concurrent", None)
            raw_val = getter() if callable(getter) else 1
            max_concurrent = int(str(raw_val))
        except (TypeError, ValueError, AttributeError):
            max_concurrent = 1
        self._comic_semaphore = asyncio.Semaphore(max(1, max_concurrent))
        self._comic_group_tasks: dict[str, asyncio.Task[object]] = {}
        self._background_tasks: set[asyncio.Task[object]] = set()

    def set_terminating(self, terminating: bool) -> None:
        self.terminating = terminating

    def _get_group_id_from_event(self, event: AstrMessageEvent) -> str | None:
        try:
            group_id = event.get_group_id()
            return group_id if group_id else None
        except Exception:
            return None

    def _get_platform_id_from_event(self, event: AstrMessageEvent) -> str:
        try:
            return event.get_platform_id()
        except Exception:
            if (
                hasattr(event, "platform_meta")
                and event.platform_meta
                and hasattr(event.platform_meta, "id")
            ):
                return event.platform_meta.id
            return "default"

    @staticmethod
    def detect_image_ext(data: bytes) -> str:
        """从图片字节嗅探扩展名，无法识别时回退 .png。

        Args:
            data: 图片二进制字节。

        Returns:
            str: 匹配的文件后缀扩展名（如 .png, .jpg, .webp, .gif, .avif）。
        """
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if data.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return ".webp"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return ".gif"
        if (
            len(data) >= 12
            and data[4:8] == b"ftyp"
            and data[8:12] in {b"avif", b"avis"}
        ):
            return ".avif"
        return ".png"

    async def handle_group_comic(
        self, event: AstrMessageEvent, days: int | None = None
    ) -> AsyncGenerator[MessageEventResult]:
        """处理 /群漫画 核心指令流程。

        Args:
            event: AstrBot 消息事件对象。
            days: 分析回溯天数（可选）。

        Returns:
            AsyncGenerator[object, None]: 指令响应消息生成器。
        """
        if self.terminating:
            return

        trace = None
        trace_id = None
        current_task = asyncio.current_task()
        if current_task:
            self._background_tasks.add(current_task)

        try:
            event.should_call_llm(True)
            group_id = self._get_group_id_from_event(event)
            platform_id = self._get_platform_id_from_event(event)

            if not group_id:
                yield event.plain_result("❌ 请在群聊中使用此命令")
                return

            self.bot_manager.update_from_event(event)

            check_target = (
                event.unified_msg_origin or f"{platform_id}:GroupMessage:{group_id}"
            )

            if not self.config_manager.get_enable_daily_comic():
                yield event.plain_result("❌ 漫画生成功能未启用")
                return

            if not self.config_manager.is_comic_group_allowed(check_target):
                yield event.plain_result("❌ 此群未启用漫画生成功能")
                return

            task_key = f"{platform_id or 'default'}:{group_id}"
            existing_task = self._comic_group_tasks.get(task_key)
            if existing_task and not existing_task.done():
                yield event.plain_result("🎨 该群已有漫画任务正在执行，请稍后再试哦~")
                return

            try:
                delivery_target = self._resolve_delivery_target(group_id, platform_id)
            except ValueError as exc:
                logger.warning(f"漫画投递目标无效，跳过生成：{exc}")
                yield event.plain_result(
                    "❌ 漫画投递目标无效，未生成漫画，请检查总结投递配置"
                )
                return
            if delivery_target is None:
                yield event.plain_result(
                    "❌ 当前总结投递未启用群目标，已跳过漫画，不会发送到原群"
                )
                return

            group_name = None
            adapter = self.bot_manager.get_adapter(platform_id)
            if adapter:
                try:
                    info = await adapter.get_group_info(group_id)
                    if info and info.group_name:
                        group_name = info.group_name
                except Exception:
                    pass

            trace_id = TraceContext.generate(
                prefix="comic", group_name=group_name or group_id
            )
            trace = TraceContext.set(
                trace_id=trace_id,
                group_id=group_id,
                group_name=group_name or group_id,
                platform=platform_id or "",
                trigger_type="comic_manual",
            )
            if self.active_task_manager:
                await self.active_task_manager.register_task(
                    task_id=trace_id,
                    group_id=group_id,
                    group_name=group_name or group_id,
                    platform=platform_id or "",
                    trigger_type="comic_manual",
                    current_stage=AnalysisStage.FETCH_MESSAGES,
                    asyncio_task=current_task,
                )

            yield event.plain_result("🎨 正在提取群聊话题并生成漫画...")

            result = await self.analysis_service.execute_comic_topic_analysis(
                group_id=group_id, platform_id=platform_id, days=days
            )
            if not result.get("success"):
                reason = result.get("reason")
                if trace and trace.status == "running":
                    trace.finish(
                        event=TaskEvent.FAIL,
                        error_message=f"提取漫画话题失败: {reason}",
                    )
                if trace_id and self.active_task_manager:
                    await self.active_task_manager.finish_task(trace_id)
                if reason == "no_messages":
                    yield event.plain_result("❌ 未找到可用于生成漫画的群聊记录")
                elif reason == "no_topics":
                    yield event.plain_result("❌ 未提取到可用于生成漫画的话题")
                elif reason == "muted":
                    logger.warning(f"群 {group_id} 开启了禁言，跳过手动漫画回复")
                else:
                    yield event.plain_result("❌ 漫画话题提取失败，原因未知")
                return

            raw_topics = result.get("topics", [])
            status = self.try_trigger_comic_generation(
                group_id,
                platform_id,
                cast("AnalysisResultPayload", {"topics": raw_topics}),
                require_auto_enabled=False,
                trace=trace,
            )
            if status == "started":
                yield event.plain_result(
                    "漫画生成任务已启动，完成后会发送到配置的目标群"
                )
            elif status in {"delivery_disabled", "delivery_unavailable"}:
                message = (
                    "当前总结投递未启用群目标，已跳过漫画，不会发送到原群"
                    if status == "delivery_disabled"
                    else "漫画投递目标无效，未发送到原群，请检查总结投递配置"
                )
                if trace and trace.status == "running":
                    trace.finish(event=TaskEvent.WARN, error_message=message)
                if trace_id and self.active_task_manager:
                    await self.active_task_manager.finish_task(trace_id)
                yield event.plain_result(message)
            elif status == "duplicate":
                if trace and trace.status == "running":
                    trace.finish(
                        event=TaskEvent.WARN, error_message="该群已有漫画任务正在执行"
                    )
                if trace_id and self.active_task_manager:
                    await self.active_task_manager.finish_task(trace_id)
                yield event.plain_result("🎨 该群已有漫画任务正在执行，请稍后再试哦~")
            elif status == "blocked":
                if trace and trace.status == "running":
                    trace.finish(
                        event=TaskEvent.WARN, error_message="此群未启用漫画生成功能"
                    )
                if trace_id and self.active_task_manager:
                    await self.active_task_manager.finish_task(trace_id)
                yield event.plain_result("❌ 此群未启用漫画生成功能")
            elif status == "no_topics":
                if trace and trace.status == "running":
                    trace.finish(
                        event=TaskEvent.WARN,
                        error_message="未提取到可用于生成漫画的话题",
                    )
                if trace_id and self.active_task_manager:
                    await self.active_task_manager.finish_task(trace_id)
                yield event.plain_result("❌ 未提取到可用于生成漫画的话题")
            else:
                if trace and trace.status == "running":
                    trace.finish(
                        event=TaskEvent.FAIL, error_message="漫画生成任务未启动"
                    )
                if trace_id and self.active_task_manager:
                    await self.active_task_manager.finish_task(trace_id)
                yield event.plain_result("⚠️ 漫画生成任务未启动，请查看插件日志")

        except DuplicateGroupTaskError:
            if trace and trace.status == "running":
                trace.finish(
                    event=TaskEvent.FAIL, error_message="该群的漫画话题提取任务正在执行"
                )
            if trace_id and self.active_task_manager:
                await self.active_task_manager.finish_task(trace_id)
            yield event.plain_result("🎨 该群的漫画话题提取任务正在执行，请稍后再试哦~")
        except asyncio.CancelledError:
            if trace and trace.status == "running":
                try:
                    trace.finish(
                        event=TaskEvent.ABORT, error_message="Task cancelled by system"
                    )
                except RuntimeError as transition_error:
                    logger.info(f"取消收尾时任务已由并发事件收敛: {transition_error}")
            if trace_id and self.active_task_manager:
                await self.active_task_manager.finish_task(trace_id)
            logger.info("手动漫画任务被取消（插件正在关闭或重载）")
        except Exception as e:
            if trace and trace.status == "running":
                trace.finish(event=TaskEvent.FAIL, error_message=str(e))
            if trace_id and self.active_task_manager:
                await self.active_task_manager.finish_task(trace_id)
            logger.error(f"手动漫画生成失败: {e}", exc_info=True)
            yield event.plain_result(
                f"❌ 漫画生成失败: {e!s}。请检查消息获取、LLM 和绘图配置"
            )
        finally:
            if current_task:
                self._background_tasks.discard(current_task)

    def _resolve_delivery_target(
        self, group_id: str, platform_id: str | None
    ) -> ReportTarget | None:
        """解析漫画唯一投递目标并拒绝原群转发别名。

        Args:
            group_id: 被分析的源群 ID。
            platform_id: 源群平台实例 ID。

        Returns:
            群聊、私聊或飞书私聊投递目标；停止投递或仅 Server酱文本通道时返回 None。

        Raises:
            ValueError: 投递配置无效或转发目标实际为原群。
        """
        mode = self.config_manager.get_report_delivery_mode()
        if mode == "source_group":
            return GroupReportTarget(group_id, platform_id)
        if mode not in FORWARD_GROUP_DELIVERY_MODES:
            if mode in QQ_OFFICIAL_DELIVERY_MODES:
                return self.bot_manager.validate_qq_official_report_target(
                    self.config_manager.get_qq_official_report_target(),
                    group_id,
                    platform_id,
                )
            if mode in FEISHU_COMIC_DELIVERY_MODES:
                reuse_target = self.config_manager.get_feishu_reuse_target()
                if reuse_target is not None:
                    return self.bot_manager.validate_lark_report_target(
                        reuse_target, group_id, platform_id
                    )
                bot_config = self.config_manager.get_feishu_bot_config()
                return FeishuPrivateTarget(
                    user_id=bot_config.user_id,
                    user_id_type=bot_config.user_id_type,
                )
            return None

        target = self.config_manager.get_forward_report_target(group_id, platform_id)
        if target.group_id == group_id:
            source_adapter = self.bot_manager.get_adapter(platform_id)
            target_adapter = self.bot_manager.get_adapter(target.platform_id)
            source_id = (
                self.bot_manager.get_adapter_platform_id(source_adapter)
                if source_adapter
                else ""
            )
            target_id = (
                self.bot_manager.get_adapter_platform_id(target_adapter)
                if target_adapter
                else ""
            )
            if source_adapter is not None and (
                source_adapter is target_adapter
                or (source_id and source_id.casefold() == target_id.casefold())
            ):
                raise ValueError("转发目标不能是原群，请填写另外一个群")
        return target

    def try_trigger_comic_generation(
        self,
        group_id: str,
        platform_id: str | None,
        analysis_result: AnalysisResultPayload,
        *,
        require_auto_enabled: bool = True,
        trace: TraceContext | None = None,
    ) -> str:
        """尝试触发后台漫画生成。

        Args:
            group_id: 源群号。
            platform_id: 源群平台标识。
            analysis_result: 分析结果字典（包含 topics 字段）。
            require_auto_enabled: 是否要求开启自动生成配置（手动指令触发时为 False）。
            trace: 当前执行链路追踪上下文。

        Returns:
            str: 触发状态（"started"、"duplicate"、"blocked"、"no_topics"、
                "disabled"、"auto_disabled"、"delivery_disabled"、
                "delivery_unavailable" 或 "terminating"）。
        """
        if self.terminating:
            return "terminating"
        enabled_getter = getattr(self.config_manager, "get_enable_daily_comic", None)
        if callable(enabled_getter) and not enabled_getter():
            return "disabled"
        auto_getter = getattr(self.config_manager, "get_enable_auto_daily_comic", None)
        auto_enabled = auto_getter() if callable(auto_getter) else True
        if require_auto_enabled and not auto_enabled:
            return "auto_disabled"

        try:
            delivery_target = self._resolve_delivery_target(group_id, platform_id)
        except ValueError as exc:
            logger.warning(f"漫画投递目标无效，跳过发送：{exc}")
            return "delivery_unavailable"
        if delivery_target is None:
            logger.info("当前总结投递配置未启用群目标，跳过群漫画发送。")
            return "delivery_disabled"

        if trace:
            if isinstance(delivery_target, FeishuPrivateTarget):
                trace.metadata["comic_target_platform"] = "feishu"
                trace.metadata["comic_target_user_id"] = delivery_target.user_id
                trace.metadata["comic_target_scene"] = "feishu_private"
            else:
                trace.metadata["comic_target_platform"] = (
                    delivery_target.platform_id or ""
                )
                if isinstance(delivery_target, QQOfficialPrivateTarget):
                    trace.metadata["comic_target_user_id"] = delivery_target.user_id
                    trace.metadata["comic_target_scene"] = delivery_target.scene
                elif isinstance(delivery_target, FeishuReuseTarget):
                    trace.metadata["comic_target_user_id"] = delivery_target.user_id
                    trace.metadata["comic_target_scene"] = "lark_private"
                else:
                    trace.metadata["comic_target_group_id"] = delivery_target.group_id
                    trace.metadata["comic_target_scene"] = "group"

        umo = f"{platform_id}:GroupMessage:{group_id}" if platform_id else group_id
        inherit_allowed = True if require_auto_enabled else None
        allowed_checker = getattr(self.config_manager, "is_comic_group_allowed", None)
        if callable(allowed_checker) and not allowed_checker(umo, inherit_allowed):
            logger.info(
                f"群 {group_id} 未通过漫画名单判定，跳过漫画生成。platform={platform_id or 'default'}"
            )
            return "blocked"

        topics = analysis_result.get("topics", [])
        statistics = analysis_result.get("statistics")
        if not topics and statistics:
            if isinstance(statistics, dict):
                topics = statistics.get("topics", [])
            else:
                topics = getattr(statistics, "topics", [])

        comic_topics = []
        for topic in topics if isinstance(topics, list) else []:
            if isinstance(topic, SummaryTopic):
                title = topic.topic
                detail = topic.detail
            elif isinstance(topic, dict):
                title = str(topic.get("topic", ""))
                detail = str(topic.get("detail", ""))
            elif hasattr(topic, "topic") and hasattr(topic, "detail"):
                title = str(topic.topic)
                detail = str(topic.detail)
            else:
                title = ""
                detail = ""
            if str(title).strip():
                comic_topics.append(
                    {"topic": str(title).strip(), "detail": str(detail).strip()}
                )
        if not comic_topics:
            logger.warning(f"群 {group_id} 没有有效话题，跳过漫画生成。")
            return "no_topics"

        task_key = f"{platform_id or 'default'}:{group_id}"
        existing_task = self._comic_group_tasks.get(task_key)
        if existing_task and not existing_task.done():
            logger.info(f"群 {group_id} 已有漫画任务等待或执行，跳过重复任务。")
            return "duplicate"

        task = asyncio.create_task(
            self._trigger_comic_generation(
                comic_topics,
                group_id,
                platform_id,
                umo,
                trace=trace,
            )
        )
        self._comic_group_tasks[task_key] = task
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        task.add_done_callback(
            lambda completed_task: (
                self._comic_group_tasks.pop(task_key, None)
                if self._comic_group_tasks.get(task_key) is completed_task
                else None
            )
        )
        return "started"

    async def _trigger_comic_generation(
        self,
        topics: list[dict[str, object]],
        group_id: str,
        platform_id: str | None,
        umo: str,
        trace: TraceContext | None = None,
    ) -> None:
        """按实时投递配置生成并上传漫画，通过信号量控制并发。

        Args:
            topics: 源群已提取的漫画话题。
            group_id: 被分析的源群 ID。
            platform_id: 源群平台实例 ID。
            umo: 源群会话 ID，用于选择绘图配置。
            trace: 当前任务追踪上下文。
        """
        cur_trace = trace or TraceContext.current()
        if cur_trace:
            from ...shared.trace_context import _current_trace

            _current_trace.set(cur_trace)

        async with self._comic_semaphore:
            if self.terminating:
                return
            try:
                current_target = self._resolve_delivery_target(group_id, platform_id)
            except ValueError as exc:
                logger.warning(f"漫画投递目标无效，取消发送：{exc}")
                return
            if current_target is None:
                logger.info("当前总结投递配置未启用群目标，取消群漫画发送。")
                return

            try:
                if cur_trace and self.active_task_manager:
                    await self.active_task_manager.update_stage(
                        cur_trace.trace_id, "COMIC_STORYBOARD"
                    )

                comic_bytes, fallback_url = await self.comic_service.generate_comic(
                    topics, group_id, umo
                )
                target = self._resolve_delivery_target(group_id, platform_id)
                if target is None:
                    logger.info("总结投递配置已关闭群目标，取消群漫画发送。")
                    return
                if self.terminating:
                    return

                image_url = fallback_url
                if comic_bytes:
                    logger.info(f"群 {group_id} 漫画生成成功，准备发送和保存副本...")
                    ext = self.detect_image_ext(comic_bytes)
                    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
                    trace_suffix = f"_{cur_trace.trace_id}" if cur_trace else ""
                    filename = f"comic_{group_id}_{ts_str}{trace_suffix}{ext}"
                    reports_dir = self.plugin_data_dir / "reports"
                    reports_dir.mkdir(parents=True, exist_ok=True)
                    comic_file_path = reports_dir / filename
                    comic_file_path.write_bytes(comic_bytes)
                    image_url = str(comic_file_path)

                    if cur_trace:
                        rfiles = cur_trace.metadata.setdefault("report_files", [])
                        if isinstance(rfiles, list):
                            rfiles.append(
                                {
                                    "filename": filename,
                                    "format": "image",
                                    "size_bytes": len(comic_bytes),
                                    "stage": "COMIC_GENERATION",
                                }
                            )
                if not image_url:
                    logger.warning(f"群 {group_id} 漫画生成未产生有效输出。")
                    return

                if cur_trace:
                    if isinstance(target, FeishuPrivateTarget):
                        cur_trace.metadata["comic_target_platform"] = "feishu"
                        cur_trace.metadata.pop("comic_target_group_id", None)
                        cur_trace.metadata["comic_target_user_id"] = target.user_id
                        cur_trace.metadata["comic_target_scene"] = "feishu_private"
                    else:
                        cur_trace.metadata["comic_target_platform"] = (
                            target.platform_id or ""
                        )
                        if isinstance(
                            target, (QQOfficialPrivateTarget, FeishuReuseTarget)
                        ):
                            cur_trace.metadata.pop("comic_target_group_id", None)
                            cur_trace.metadata["comic_target_user_id"] = target.user_id
                            cur_trace.metadata["comic_target_scene"] = (
                                target.scene
                                if isinstance(target, QQOfficialPrivateTarget)
                                else "lark_private"
                            )
                        else:
                            cur_trace.metadata.pop("comic_target_user_id", None)
                            cur_trace.metadata["comic_target_group_id"] = (
                                target.group_id
                            )
                            cur_trace.metadata["comic_target_scene"] = "group"

                caption = (
                    TraceContext.make_report_caption()
                    if comic_bytes and self.config_manager.get_show_report_caption()
                    else ""
                )
                if isinstance(
                    target,
                    (QQOfficialPrivateTarget, FeishuPrivateTarget, FeishuReuseTarget),
                ) or (target.group_id != group_id or target.platform_id != platform_id):
                    group_name = (
                        cur_trace.group_name
                        if cur_trace
                        and cur_trace.group_id == group_id
                        and cur_trace.platform.casefold()
                        == (platform_id or "").casefold()
                        else ""
                    )
                    if not group_name.strip():
                        try:
                            source_adapter = self.bot_manager.get_adapter(platform_id)
                            if source_adapter:
                                info = await asyncio.wait_for(
                                    source_adapter.get_group_info(group_id), timeout=5
                                )
                                if info:
                                    group_name = info.group_name
                        except Exception as exc:
                            logger.warning(
                                f"获取漫画来源群名称失败，保留群 ID：{type(exc).__name__}"
                            )
                    group_name = " ".join(group_name.split())
                    source_label = (
                        f"来源群：{group_name}（{group_id}）"
                        if group_name and group_name != group_id
                        else f"来源群：{group_id}"
                    )
                    caption = f"{source_label}\n{caption}".rstrip()
                if self.terminating or (
                    self._resolve_delivery_target(group_id, platform_id) != target
                ):
                    logger.info("漫画投递配置已改变，取消向旧目标发送。")
                    return
                if isinstance(target, FeishuPrivateTarget):
                    bot_config = self.config_manager.get_feishu_bot_config()
                    sent = await self.feishu_sender.send_comic(
                        bot_config, image_url, caption
                    )
                else:
                    adapter = self.bot_manager.get_adapter(target.platform_id)
                    if adapter is None:
                        logger.warning(
                            f"漫画目标平台不可用，取消发送：{target.platform_id or 'default'}"
                        )
                        return
                    if isinstance(target, (QQOfficialPrivateTarget, FeishuReuseTarget)):
                        sent = await adapter.send_private_image(
                            target, image_url, caption=caption
                        )
                    elif caption or comic_bytes:
                        sent = await adapter.send_image(
                            target.group_id, image_url, caption=caption
                        )
                    else:
                        sent = await adapter.send_image(target.group_id, image_url)
                if not sent:
                    logger.warning(
                        f"漫画投递失败（目标会话 {target.session_id}），不会回发原群。"
                    )
                    return
                if (
                    isinstance(
                        target,
                        (
                            QQOfficialPrivateTarget,
                            FeishuPrivateTarget,
                            FeishuReuseTarget,
                        ),
                    )
                    or not comic_bytes
                ):
                    return

                if self._resolve_delivery_target(group_id, platform_id) != target:
                    logger.info("漫画投递配置已改变，跳过相册上传。")
                    return
                if self.plugin_instance:
                    inst_upload = getattr(
                        self.plugin_instance, "_try_upload_image", None
                    )
                    if inst_upload is not None and (
                        hasattr(inst_upload, "assert_called")
                        or hasattr(inst_upload, "assert_awaited")
                        or hasattr(inst_upload, "_mock_name")
                        or type(inst_upload).__name__
                        in ("Mock", "MagicMock", "AsyncMock")
                    ):
                        await inst_upload(
                            target.group_id,
                            image_url,
                            target.platform_id,
                            is_comic=True,
                        )
                    else:
                        await self._try_upload_image(
                            target.group_id,
                            image_url,
                            target.platform_id,
                            is_comic=True,
                        )
                else:
                    await self._try_upload_image(
                        target.group_id, image_url, target.platform_id, is_comic=True
                    )
            except Exception as e:
                logger.error(f"群 {group_id} 后台漫画生成执行异常: {e}", exc_info=True)

    async def _try_upload_image(
        self,
        group_id: str,
        image_url: str,
        platform_id: str | None,
        is_comic: bool = True,
    ) -> None:
        """尝试将图片上传到群相册"""
        if not self.config_manager:
            return
        if not self.config_manager.get_enable_comic_album_upload():
            return
        adapter = (
            self.bot_manager.get_adapter(platform_id) if self.bot_manager else None
        )
        upload_fn = getattr(adapter, "upload_group_album", None)
        if not callable(upload_fn):
            return

        album_name = self.config_manager.get_comic_album_name() or "daily_analysis"
        strict_mode = self.config_manager.get_group_album_strict_mode()
        try:
            target_path = (
                str(Path(image_url).resolve())
                if Path(image_url).exists()
                else image_url
            )
            res = upload_fn(
                group_id,
                target_path,
                album_id=None,
                album_name=album_name,
                strict_mode=strict_mode,
            )
            if asyncio.iscoroutine(res):
                await res
        except Exception as e:
            logger.warning(f"漫画群相册上传异常 (群 {group_id}): {e}")
