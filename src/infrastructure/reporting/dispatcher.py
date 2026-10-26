from __future__ import annotations

import asyncio
import base64
import os
import tempfile
import time
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from ...domain.value_objects.report_delivery import (
    FEISHU_DELIVERY_MODES,
    FORWARD_GROUP_DELIVERY_MODES,
    QQ_OFFICIAL_DELIVERY_MODES,
    SERVERCHAN_DELIVERY_MODES,
    ChatReportTarget,
    FeishuReuseTarget,
    GroupReportTarget,
    QQOfficialPrivateTarget,
)
from ...shared.constants import AnalysisStage
from ...shared.trace_context import TraceContext
from ...utils.logger import logger
from ..messaging.feishu_bot_sender import FeishuBotSender
from ..messaging.serverchan_bot_sender import ServerChanBotSender
from .html_renderer import bind_group_html_renderer

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from ...domain.repositories.platform_adapter_repository import (
        GroupAlbumSupportProtocol,
        GroupFileSupportProtocol,
    )
    from ...domain.value_objects import AnalysisResultPayload
    from ..config.config_manager import ConfigManager
    from ..messaging.message_sender import MessageSender
    from ..platform.adapters.onebot_adapter import OneBotAdapter
    from .generators import ReportGenerator


class ReportDispatcher:
    """
    报告分发器
    负责协调报告生成、格式选择、消息发送和失败重试
    """

    config_manager: ConfigManager
    report_generator: ReportGenerator | None
    message_sender: MessageSender
    serverchan_sender: ServerChanBotSender
    feishu_sender: FeishuBotSender

    def __init__(
        self,
        config_manager: ConfigManager,
        report_generator: ReportGenerator | None,
        message_sender: MessageSender,
    ) -> None:
        self.config_manager = config_manager
        self.report_generator = report_generator
        self.message_sender = message_sender
        self.serverchan_sender = ServerChanBotSender()
        self.feishu_sender = FeishuBotSender()
        self._html_render_func: Callable[..., Awaitable[str | bytes | None]] | None = (
            None
        )

    def set_html_render(
        self, render_func: Callable[..., Awaitable[str | bytes | None]]
    ):
        """设置 HTML 渲染函数 (运行时注入)"""
        self._html_render_func = render_func

    def _is_qq_official(self, platform_id: str | None) -> bool:
        adapter = self.message_sender.bot_manager.get_adapter(platform_id)
        return bool(adapter and adapter.get_platform_name() == "qq_official")

    # 复用多平台通用 Markdown 结构化排版的平台（如 Telegram、Discord、Satori）：
    # 身份自动降级为昵称文本，排版与结构化报告一致。
    _SHARED_MARKDOWN_PLATFORMS = frozenset({"telegram", "discord", "satori"})

    def _uses_shared_markdown_report(self, platform_id: str | None) -> bool:
        """平台是否复用 QQ 官方的 Markdown 文本排版。"""
        adapter = self.message_sender.bot_manager.get_adapter(platform_id)
        return bool(
            adapter and adapter.get_platform_name() in self._SHARED_MARKDOWN_PLATFORMS
        )

    @staticmethod
    def _target_trace_payload(target: ChatReportTarget) -> dict[str, str]:
        """生成各报告格式共用的投递目标追踪字段。

        Args:
            target: 已校验的群聊或 QQ 官方私聊目标。

        Returns:
            含目标场景及群 ID 或用户 ID 的字段。
        """
        if isinstance(target, QQOfficialPrivateTarget):
            return {
                "target_scene": target.scene,
                "target_user_id": target.user_id,
            }
        return {"target_scene": "group", "target_group_id": target.group_id}

    async def dispatch(
        self,
        group_id: str,
        analysis_result: AnalysisResultPayload,
        platform_id: str | None = None,
        *,
        output_formats: list[str] | None = None,
    ) -> bool:
        """按独立开关分发报告，各通道失败互不阻断。

        Args:
            group_id: 被分析的源群 ID，用于生成报告及历史归档。
            analysis_result: 源群分析结果。
            platform_id: 源群平台实例 ID。
            output_formats: 本次使用的格式；手动入口仅传首个格式。

        Returns:
            至少一个通道成功，或配置要求跳过投递时返回 True。
        """
        trace_id = TraceContext.get()
        trace = TraceContext.current()
        try:
            mode = self.config_manager.get_report_delivery_mode()
        except ValueError as exc:
            logger.error(f"[{trace_id}] 总结投递配置无效：{exc}")
            return False
        if trace:
            trace.metadata["report_destination"] = mode
        if mode == "disabled":
            if trace:
                trace.metadata["report_delivery_skipped"] = True
                trace.metadata["report_delivery_results"] = {}
            logger.info(f"[{trace_id}] 群 {group_id} 按总结投递配置跳过发送")
            return True

        source_label = ""
        if mode != "source_group":
            # 仅复用同一源群、同一平台的上下文，避免并发或跨平台群号混淆。
            group_name = (
                trace.group_name
                if trace
                and trace.group_id == group_id
                and trace.platform.casefold() == (platform_id or "").casefold()
                else ""
            )
            if not group_name.strip():
                try:
                    source_adapter = self.message_sender.bot_manager.get_adapter(
                        platform_id
                    )
                    if source_adapter:
                        info = await asyncio.wait_for(
                            source_adapter.get_group_info(group_id), timeout=5
                        )
                        if info:
                            group_name = info.group_name
                except Exception as exc:
                    logger.warning(
                        f"[{trace_id}] 获取来源群名称失败，保留群 ID：{type(exc).__name__}"
                    )
            group_name = " ".join(group_name.split())
            source_label = (
                f"来源群：{group_name}（{group_id}）"
                if group_name and group_name != group_id
                else f"来源群：{group_id}"
            )

        forward_enabled = mode in FORWARD_GROUP_DELIVERY_MODES
        qq_official_enabled = mode in QQ_OFFICIAL_DELIVERY_MODES
        serverchan_enabled = mode in SERVERCHAN_DELIVERY_MODES
        feishu_enabled = mode in FEISHU_DELIVERY_MODES
        channel_results: dict[str, bool] = {}
        group_channels: list[str] = []
        if mode == "source_group" or forward_enabled:
            group_channels.append("group")
        if qq_official_enabled:
            group_channels.append("qq_official")
        for channel in group_channels:
            try:
                if channel == "qq_official":
                    target = self.message_sender.bot_manager.validate_qq_official_report_target(
                        self.config_manager.get_qq_official_report_target(),
                        group_id,
                        platform_id,
                    )
                else:
                    target = (
                        self.config_manager.get_forward_report_target(
                            group_id, platform_id
                        )
                        if forward_enabled
                        else GroupReportTarget(group_id, platform_id)
                    )
                    if (
                        channel == "group"
                        and forward_enabled
                        and target.group_id == group_id
                    ):
                        bot_manager = self.message_sender.bot_manager
                        source_adapter = bot_manager.get_adapter(platform_id)
                        target_adapter = bot_manager.get_adapter(target.platform_id)
                        source_id = (
                            bot_manager.get_adapter_platform_id(source_adapter)
                            if source_adapter
                            else ""
                        )
                        target_id = (
                            bot_manager.get_adapter_platform_id(target_adapter)
                            if target_adapter
                            else ""
                        )
                        if source_adapter is not None and (
                            source_adapter is target_adapter
                            or (
                                source_id
                                and source_id.casefold() == target_id.casefold()
                            )
                        ):
                            raise ValueError("转发目标不能是原群，请填写另外一个群")
                channel_results[channel] = await self._dispatch_group_reports(
                    group_id,
                    analysis_result,
                    platform_id,
                    target,
                    output_formats,
                    source_label=source_label,
                )
            except Exception as exc:
                detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                logger.error(f"[{trace_id}] 群总结投递失败（{channel}）：{detail}")
                channel_results[channel] = False

        if serverchan_enabled:
            try:
                channel_results["serverchan"] = await self._dispatch_serverchan(
                    group_id, analysis_result, source_label=source_label
                )
            except Exception as exc:
                # 机器人请求地址包含令牌，禁止输出异常正文及堆栈。
                logger.error(f"[{trace_id}] Server酱总结投递异常：{type(exc).__name__}")
                channel_results["serverchan"] = False

        if feishu_enabled:
            try:
                channel_results["feishu"] = await self._dispatch_feishu(
                    group_id,
                    analysis_result,
                    platform_id,
                    source_label=source_label,
                )
            except Exception as exc:
                # 应用密钥在请求体中出现，禁止输出异常正文及堆栈。
                logger.error(f"[{trace_id}] 飞书总结投递异常：{type(exc).__name__}")
                channel_results["feishu"] = False

        if trace:
            trace.metadata["report_delivery_results"] = channel_results
        sent_any = any(channel_results.values())
        if sent_any and not all(channel_results.values()):
            logger.warning(
                f"[{trace_id}] 总结部分投递成功，各通道结果：{channel_results}"
            )
        return sent_any

    async def _dispatch_serverchan(
        self,
        group_id: str,
        analysis_result: AnalysisResultPayload,
        *,
        source_label: str = "",
    ) -> bool:
        """独立生成并推送含来源标识的文本总结。"""
        bot_config = self.config_manager.get_serverchan_bot_config()
        if not self.report_generator:
            logger.error("[分发器] 报告生成器未初始化，无法推送 Server酱总结")
            return False
        text = self.report_generator.generate_text_report(analysis_result)
        trace = TraceContext.current()
        with (
            trace.span(
                AnalysisStage.DISPATCH_REPORT,
                {
                    "platform": "serverchan",
                    "group_id": group_id,
                    "target_chat_id": str(bot_config.chat_id),
                    "formats": ["text"],
                    "format": "text",
                },
            )
            if trace
            else nullcontext()
        ) as dispatch_span:
            sent = await self.serverchan_sender.send_summary(
                bot_config,
                f"📊 每日群聊分析报告\n{source_label or f'来源群：{group_id}'}\n\n{text}",
            )
            if dispatch_span:
                dispatch_span.setdefault("payload", {})["success"] = sent
            return sent

    async def _dispatch_feishu(
        self,
        group_id: str,
        analysis_result: AnalysisResultPayload,
        platform_id: str | None = None,
        *,
        source_label: str = "",
    ) -> bool:
        """按配置投递飞书总结：优先复用宿主飞书实例，否则独立凭据直连。"""
        reuse_target = self.config_manager.get_feishu_reuse_target()
        if reuse_target is not None:
            return await self._dispatch_feishu_reuse(
                group_id,
                analysis_result,
                platform_id,
                reuse_target,
                source_label=source_label,
            )
        bot_config = self.config_manager.get_feishu_bot_config()
        if not self.report_generator:
            logger.error("[分发器] 报告生成器未初始化，无法推送飞书总结")
            return False
        text = self.report_generator.generate_text_report(analysis_result)
        trace = TraceContext.current()
        with (
            trace.span(
                AnalysisStage.DISPATCH_REPORT,
                {
                    "platform": "feishu",
                    "group_id": group_id,
                    "target_user_id": bot_config.user_id,
                    "formats": ["text"],
                    "format": "text",
                },
            )
            if trace
            else nullcontext()
        ) as dispatch_span:
            sent = await self.feishu_sender.send_summary(
                bot_config,
                f"📊 每日群聊分析报告\n{source_label or f'来源群：{group_id}'}\n\n{text}",
            )
            if dispatch_span:
                dispatch_span.setdefault("payload", {})["success"] = sent
            return sent

    async def _dispatch_feishu_reuse(
        self,
        group_id: str,
        analysis_result: AnalysisResultPayload,
        source_platform_id: str | None,
        reuse_target: GroupReportTarget | FeishuReuseTarget,
        *,
        source_label: str = "",
    ) -> bool:
        """通过宿主飞书(Lark)实例投递总结，群聊走完整格式路径，私聊发送分段文本。"""
        bot_manager = self.message_sender.bot_manager
        reuse_target = bot_manager.validate_lark_report_target(
            reuse_target, group_id, source_platform_id
        )
        if isinstance(reuse_target, GroupReportTarget):
            return await self._dispatch_group_reports(
                group_id,
                analysis_result,
                reuse_target.platform_id,
                reuse_target,
                self.config_manager.get_output_format(),
                source_label=source_label,
            )
        adapter = bot_manager.get_adapter(reuse_target.platform_id)
        if adapter is None:
            logger.error(f"[分发器] 飞书目标平台不可用：{reuse_target.platform_id}")
            return False
        if not self.report_generator:
            logger.error("[分发器] 报告生成器未初始化，无法推送飞书总结")
            return False
        text = self.report_generator.generate_text_report(analysis_result)
        trace = TraceContext.current()
        with (
            trace.span(
                AnalysisStage.DISPATCH_REPORT,
                {
                    "platform": "feishu",
                    "group_id": group_id,
                    "target_user_id": reuse_target.user_id,
                    "formats": ["text"],
                    "format": "text",
                },
            )
            if trace
            else nullcontext()
        ) as dispatch_span:
            sent = await adapter.send_private_text_report(
                reuse_target,
                f"📊 每日群聊分析报告\n{source_label or f'来源群：{group_id}'}\n\n{text}",
            )
            if dispatch_span:
                dispatch_span.setdefault("payload", {})["success"] = sent
            return sent

    async def _dispatch_group_reports(
        self,
        group_id: str,
        analysis_result: AnalysisResultPayload,
        platform_id: str | None,
        target: ChatReportTarget,
        output_formats: list[str] | None,
        *,
        source_label: str = "",
    ) -> bool:
        """按源群生成报告，各格式和回退均保留目标及来源标识。"""
        trace_id = TraceContext.get()
        if output_formats is None:
            output_formats = self.config_manager.get_output_format()
        if isinstance(output_formats, str):
            output_formats = [output_formats]

        logger.info(
            f"[{trace_id}] 正在分发群 {group_id} 的报告到会话 {target.session_id} "
            f"(格式: {', '.join(output_formats)})"
        )

        dispatch_map = {
            "image": self._dispatch_image,
            "html": self._dispatch_html,
            "text": self._dispatch_text,
        }
        sent_any = False
        format_results: dict[str, bool] = {}

        for fmt in output_formats:
            handler = dispatch_map.get(fmt)
            if not handler:
                logger.warning(f"[{trace_id}] 不支持的报告格式: {fmt}")
                continue
            try:
                ok = bool(
                    await handler(
                        group_id,
                        analysis_result,
                        platform_id,
                        target=target,
                        source_label=source_label,
                    )
                )
                format_results[fmt] = ok
                sent_any = ok or sent_any
            except Exception as e:
                logger.error(
                    f"[{trace_id}] 群 {group_id} 的 {fmt} 报告发送异常: {e}",
                    exc_info=True,
                )
                format_results[fmt] = False

        if sent_any:
            logger.info(
                f"[{trace_id}] 群 {group_id} 的报告分发完成，至少一种格式发送成功"
            )
        else:
            logger.error(f"[{trace_id}] 群 {group_id} 的报告分发失败，未发送任何报告")
        return sent_any

    async def _dispatch_image(
        self,
        group_id: str,
        analysis_result: AnalysisResultPayload,
        platform_id: str | None,
        *,
        target: ChatReportTarget | None = None,
        source_label: str = "",
    ) -> bool:
        trace_id = TraceContext.get()
        trace_ctx = TraceContext.current()
        delivery_target = target or GroupReportTarget(group_id, platform_id)
        if not self.report_generator:
            logger.error(f"[{trace_id}] 报告生成器未初始化，无法生成图片报告。")
            return False

        # 1. 检查渲染函数
        if not self._html_render_func:
            logger.warning(f"[{trace_id}] 未设置 HTML 渲染函数，回退到文本模式。")
            return await self._dispatch_text(
                group_id,
                analysis_result,
                platform_id,
                target=delivery_target,
                source_label=source_label,
            )

        # 2. 生成图片
        html_render = bind_group_html_renderer(
            self._html_render_func, group_id, platform_id
        )
        image_url = None
        try:
            # 定义头像获取回调，请求小尺寸头像以优化性能
            async def avatar_url_getter(
                user_id: str, size: int | None = None
            ) -> str | None:
                if not platform_id:
                    return None
                adapter = self.message_sender.bot_manager.get_adapter(platform_id)
                if adapter:
                    return await adapter.get_user_avatar_url(user_id, size=size or 40)
                return None

            trace = TraceContext.current()
            override_theme = (
                str(trace.metadata.get("override_template_name") or "").strip()
                if trace
                else ""
            )
            template_theme: str = (
                override_theme
                or self.config_manager.get_report_template()
                or "scrapbook"
            )

            if trace:
                with trace.span(
                    AnalysisStage.RENDER_REPORT,
                    {"format": "image", "template": template_theme},
                ):
                    (
                        image_url,
                        _html_content,
                    ) = await self.report_generator.generate_image_report(
                        analysis_result,
                        group_id,
                        html_render,
                        avatar_url_getter=avatar_url_getter,
                        avatar_cache_namespace=platform_id,
                        allow_alphanumeric_user_ids=self._is_qq_official(platform_id),
                        template_theme=template_theme,
                    )
            else:
                (
                    image_url,
                    _html_content,
                ) = await self.report_generator.generate_image_report(
                    analysis_result,
                    group_id,
                    html_render,
                    avatar_url_getter=avatar_url_getter,
                    avatar_cache_namespace=platform_id,
                    allow_alphanumeric_user_ids=self._is_qq_official(platform_id),
                    template_theme=template_theme,
                )
        except Exception as e:
            logger.error(f"[{trace_id}] Failed to generate image report: {e}")
            # image_url and html_content remain None

        # 4. 发送图片
        sent = False
        dest_filename: str | None = None
        if image_url:
            with (
                trace_ctx.span(
                    AnalysisStage.DISPATCH_REPORT,
                    {
                        "platform": delivery_target.platform_id or "auto",
                        "group_id": group_id,
                        "source_platform": platform_id,
                        **self._target_trace_payload(delivery_target),
                        "formats": ["image"],
                        "format": "image",
                    },
                )
                if trace_ctx
                else nullcontext()
            ) as dispatch_span:
                try:
                    reports_dir = self.report_generator.data_dir / "reports"
                    reports_dir.mkdir(parents=True, exist_ok=True)
                    eff_trace_id = (
                        trace_id
                        or (trace_ctx.trace_id if trace_ctx else "")
                        or TraceContext.get()
                        or ""
                    )
                    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
                    filename = (
                        f"report_{group_id}_{ts_str}_{eff_trace_id}.jpg"
                        if eff_trace_id
                        else f"report_{group_id}_{ts_str}.jpg"
                    )
                    dest = reports_dir / filename
                    dest_filename = dest.name
                    if os.path.exists(image_url):
                        import shutil

                        shutil.copy2(image_url, dest)
                    elif image_url.startswith("base64://"):
                        data = base64.b64decode(image_url[9:])
                        dest.write_bytes(data)

                    # 关联到 TraceContext 并在数据库中更新元数据
                    if trace_ctx:
                        rfiles = trace_ctx.metadata.setdefault("report_files", [])
                        if isinstance(rfiles, list) and not any(
                            isinstance(rf, dict) and rf.get("filename") == dest.name
                            for rf in rfiles
                        ):
                            rfiles.append(
                                {
                                    "filename": dest.name,
                                    "path": str(dest.resolve()),
                                    "format": "image",
                                    "size_bytes": dest.stat().st_size
                                    if dest.exists()
                                    else 0,
                                    "created_at": time.time(),
                                }
                            )
                        from ...shared.trace_context import _global_trace_store

                        if _global_trace_store is not None:
                            try:
                                _global_trace_store.save_trace(trace_ctx.to_dict())
                            except Exception:
                                pass
                except Exception as e:
                    logger.warning(f"[{trace_id}] 保存历史报告副本失败: {e}")

                caption = (
                    TraceContext.make_report_caption()
                    if self.config_manager.get_show_report_caption()
                    else ""
                )
                if source_label:
                    caption = f"{source_label}\n{caption}".rstrip()
                is_b64 = image_url.startswith("base64://")
                raw_image_kb = 0.0
                base64_payload_kb = 0.0
                bloat_ratio = None
                if is_b64:
                    b64_str = image_url[9:]
                    b64_len = len(b64_str)
                    base64_payload_kb = round(b64_len / 1024, 2)
                    padding = b64_str.count("=", max(0, b64_len - 2))
                    raw_bytes_len = max(0, (b64_len * 3 // 4) - padding)
                    raw_image_kb = round(raw_bytes_len / 1024, 2)
                    bloat_pct = round(
                        ((b64_len - raw_bytes_len) / max(raw_bytes_len, 1)) * 100, 1
                    )
                    bloat_ratio = f"+{bloat_pct}%"
                elif os.path.exists(image_url):
                    raw_image_kb = round(os.path.getsize(image_url) / 1024, 2)

                dispatch_start_ts = time.perf_counter()
                try:
                    if isinstance(delivery_target, QQOfficialPrivateTarget):
                        adapter = self.message_sender.bot_manager.get_adapter(
                            delivery_target.platform_id
                        )
                        sent = bool(
                            adapter
                            and await adapter.send_private_image(
                                delivery_target, image_url, caption=caption
                            )
                        )
                    else:
                        sent = await self.message_sender.send_image_smart(
                            delivery_target.group_id,
                            image_url,
                            caption,
                            delivery_target.platform_id,
                        )
                except Exception as e:
                    logger.error(f"[{trace_id}] 图片报告发送异常: {e}", exc_info=True)
                dispatch_api_ms = round(
                    (time.perf_counter() - dispatch_start_ts) * 1000, 2
                )

                # 5. 群聊才备份到群文件或群相册，私聊严格不调用群接口。
                if isinstance(delivery_target, GroupReportTarget):
                    try:
                        await self._try_upload_image(
                            delivery_target.group_id,
                            image_url,
                            delivery_target.platform_id,
                        )
                    except Exception as e:
                        logger.warning(
                            f"[{trace_id}] 图片报告备份失败，不影响发送状态: {e}"
                        )

                if dispatch_span:
                    dispatch_span.setdefault("payload", {}).update(
                        {
                            "platform": delivery_target.platform_id or "auto",
                            "formats": ["image"],
                            "format": "image",
                            "transmission_mode": "base64" if is_b64 else "file_path",
                            "raw_image_kb": raw_image_kb,
                            "base64_payload_kb": base64_payload_kb,
                            "bloat_ratio": bloat_ratio,
                            "dispatch_api_ms": dispatch_api_ms,
                            "success": bool(sent),
                            "image_sent": bool(sent),
                            "report_file": dest_filename,
                        }
                    )
                    if not sent:
                        dispatch_span["status"] = "warning"
                        dispatch_span["payload"]["warning"] = (
                            "图片报告发送失败，已自动降级回退至文本报告"
                        )
                        if trace_ctx:
                            trace_ctx.metadata["has_warnings"] = True

        if sent:
            return True

        # 6. 最终回退：如果图片发送失败（包括生成失败或发送接口报错），直接尝试发送文本报告
        logger.warning(
            f"[{trace_id}] Image dispatch failed, falling back to text report."
        )
        return await self._dispatch_text(
            group_id,
            analysis_result,
            platform_id,
            target=delivery_target,
            source_label=source_label,
        )

    async def _dispatch_html(
        self,
        group_id: str,
        analysis_result: AnalysisResultPayload,
        platform_id: str | None,
        *,
        target: ChatReportTarget | None = None,
        source_label: str = "",
    ) -> bool:
        trace_id = TraceContext.get()
        trace_ctx = TraceContext.current()
        delivery_target = target or GroupReportTarget(group_id, platform_id)
        if not self.report_generator:
            logger.error(f"[{trace_id}] 报告生成器未初始化，无法生成 HTML 报告。")
            return False

        html_path = None
        try:

            async def avatar_url_getter(
                user_id: str, size: int | None = None
            ) -> str | None:
                if not platform_id:
                    return None
                adapter = self.message_sender.bot_manager.get_adapter(platform_id)
                if adapter:
                    return await adapter.get_user_avatar_url(user_id, size=size or 40)
                return None

            trace = TraceContext.current()
            override_theme = (
                str(trace.metadata.get("override_template_name") or "").strip()
                if trace
                else ""
            )
            template_theme: str = (
                override_theme
                or self.config_manager.get_report_template()
                or "scrapbook"
            )

            if trace:
                with trace.span(
                    AnalysisStage.RENDER_REPORT,
                    {"format": "html", "template": template_theme},
                ):
                    (
                        html_path,
                        _json_path,
                    ) = await self.report_generator.generate_html_report(
                        analysis_result,
                        group_id,
                        avatar_url_getter=avatar_url_getter,
                        avatar_cache_namespace=platform_id,
                        allow_alphanumeric_user_ids=self._is_qq_official(platform_id),
                        template_theme=template_theme,
                        trace_id=trace_id,
                    )
            else:
                (
                    html_path,
                    _json_path,
                ) = await self.report_generator.generate_html_report(
                    analysis_result,
                    group_id,
                    avatar_url_getter=avatar_url_getter,
                    avatar_cache_namespace=platform_id,
                    allow_alphanumeric_user_ids=self._is_qq_official(platform_id),
                    template_theme=template_theme,
                    trace_id=trace_id,
                )
        except Exception as e:
            logger.error(f"[{trace_id}] Failed to generate HTML report: {e}")

        html_filename: str | None = None
        sent = False
        if html_path:
            with (
                trace_ctx.span(
                    AnalysisStage.DISPATCH_REPORT,
                    {
                        "platform": delivery_target.platform_id or "auto",
                        "group_id": group_id,
                        "source_platform": platform_id,
                        **self._target_trace_payload(delivery_target),
                        "formats": ["html"],
                        "format": "html",
                    },
                )
                if trace_ctx
                else nullcontext()
            ) as dispatch_span:
                try:
                    html_file = Path(html_path)
                    html_filename = html_file.name
                    if trace_ctx:
                        rfiles = trace_ctx.metadata.setdefault("report_files", [])
                        if isinstance(rfiles, list) and not any(
                            isinstance(rf, dict)
                            and rf.get("filename") == html_file.name
                            for rf in rfiles
                        ):
                            rfiles.append(
                                {
                                    "filename": html_file.name,
                                    "path": str(html_file.resolve()),
                                    "format": "html",
                                    "size_bytes": html_file.stat().st_size
                                    if html_file.exists()
                                    else 0,
                                    "created_at": time.time(),
                                }
                            )
                        from ...shared.trace_context import _global_trace_store

                        if _global_trace_store is not None:
                            try:
                                _global_trace_store.save_trace(trace_ctx.to_dict())
                            except Exception:
                                pass
                except Exception as e:
                    logger.warning(f"[{trace_id}] 关联 HTML 报告元数据失败: {e}")

                is_only_url = self.config_manager.get_html_only_url()
                base_url = self.config_manager.get_html_base_url()

                if is_only_url:
                    if base_url and base_url.strip():
                        # 获取配置的目录
                        html_output_dir = self.config_manager.get_html_output_dir()

                        # 若用户配置为空，使用默认目录
                        if not html_output_dir:
                            html_output_dir = str(
                                self.report_generator.data_dir
                                / "self_hosted_html_reports"
                            )

                        # 计算相对路径并转换为URL
                        rel_path = os.path.relpath(html_path, html_output_dir)
                        url_path = rel_path.replace(os.sep, "/")
                        encoded_url_path = quote(url_path.lstrip("/"), safe="/")
                        report_url = f"{base_url.rstrip('/')}/{encoded_url_path}"

                        if isinstance(delivery_target, QQOfficialPrivateTarget):
                            adapter = self.message_sender.bot_manager.get_adapter(
                                delivery_target.platform_id
                            )
                            sent = bool(
                                adapter
                                and await adapter.send_private_text(
                                    delivery_target,
                                    (f"{source_label}\n" if source_label else "")
                                    + f"📊 今日群聊分析报告已生成：\n{report_url}",
                                )
                            )
                        else:
                            sent = await self.message_sender.send_text(
                                delivery_target.group_id,
                                (f"{source_label}\n" if source_label else "")
                                + f"📊 今日群聊分析报告已生成：\n{report_url}",
                                delivery_target.platform_id,
                            )
                    else:
                        logger.warning(
                            f"[{trace_id}] 群 {group_id} 开启了仅发送外链，但未配置 html_base_url，已进行降级，回退至发送 HTML 文件。"
                        )

                if not sent:
                    caption = (
                        self.report_generator.build_html_caption(html_path)
                        if self.config_manager.get_show_report_caption()
                        else ""
                    )
                    if source_label:
                        caption = f"{source_label}\n{caption}".rstrip()
                    if isinstance(delivery_target, QQOfficialPrivateTarget):
                        adapter = self.message_sender.bot_manager.get_adapter(
                            delivery_target.platform_id
                        )
                        sent = bool(
                            adapter
                            and await adapter.send_private_file(
                                delivery_target,
                                html_path,
                                Path(html_path).name,
                                caption=caption,
                            )
                        )
                    else:
                        sent = await self.message_sender.send_file(
                            delivery_target.group_id,
                            html_path,
                            caption=caption,
                            platform_id=delivery_target.platform_id,
                        )

                if dispatch_span:
                    dispatch_span.setdefault("payload", {}).update(
                        {
                            "platform": delivery_target.platform_id or "auto",
                            "formats": ["html"],
                            "format": "html",
                            "success": bool(sent),
                            "html_sent": bool(sent),
                            "html_file": html_filename,
                        }
                    )
                    if not sent:
                        dispatch_span["status"] = "warning"
                        dispatch_span["payload"]["warning"] = (
                            "HTML 报告发送失败，已自动降级回退至文本报告"
                        )
                        if trace_ctx:
                            trace_ctx.metadata["has_warnings"] = True

                if sent:
                    return True

        logger.warning(
            f"[{trace_id}] HTML dispatch failed, falling back to text report."
        )
        return await self._dispatch_text(
            group_id,
            analysis_result,
            platform_id,
            target=delivery_target,
            source_label=source_label,
        )

    async def _dispatch_text(
        self,
        group_id: str,
        analysis_result: AnalysisResultPayload,
        platform_id: str | None,
        *,
        target: ChatReportTarget | None = None,
        source_label: str = "",
    ) -> bool:
        """生成源群的文本报告并发送到指定目标。

        Args:
            group_id: 被分析的源群 ID。
            analysis_result: 源群分析结果。
            platform_id: 源群平台实例 ID。
            target: 实际投递目标，省略时沿用源群。

        Returns:
            实际发送成功时返回 True。
        """
        delivery_target = target or GroupReportTarget(group_id, platform_id)
        logger.info(f"[分发器] 正在向会话 {delivery_target.session_id} 分发文本报告")
        trace_ctx = TraceContext.current()
        if not self.report_generator:
            logger.error("[分发器] 报告生成器未初始化，无法生成文本报告。")
            return False
        is_qq_official = self._is_qq_official(delivery_target.platform_id)
        fallback_report = None
        if isinstance(delivery_target, QQOfficialPrivateTarget):
            text_report = self.report_generator.generate_text_report(analysis_result)
        elif is_qq_official:
            (
                text_report,
                fallback_report,
            ) = await self.report_generator.generate_markdown_report(
                analysis_result,
                bind_group_html_renderer(self._html_render_func, group_id, platform_id),
                mention_style="qq"
                if platform_id == delivery_target.platform_id
                else "name",
            )
        elif self._uses_shared_markdown_report(delivery_target.platform_id):
            # 跨平台报告使用昵称，避免把源平台的用户 ID 当成目标平台账号。
            text_report, _ = await self.report_generator.generate_markdown_report(
                analysis_result, mention_style="name"
            )
        else:
            text_report = self.report_generator.generate_text_report(analysis_result)
        if source_label:
            text_report = f"{source_label}\n\n{text_report}"
            if fallback_report is not None:
                fallback_report = f"{source_label}\n\n{fallback_report}"
        adapter = self.message_sender.bot_manager.get_adapter(
            delivery_target.platform_id
        )

        with (
            trace_ctx.span(
                AnalysisStage.DISPATCH_REPORT,
                {
                    "platform": delivery_target.platform_id or "auto",
                    "group_id": group_id,
                    **self._target_trace_payload(delivery_target),
                    "formats": ["text"],
                    "format": "text",
                },
            )
            if trace_ctx
            else nullcontext()
        ) as dispatch_span:
            sent = False
            try:
                if isinstance(delivery_target, QQOfficialPrivateTarget):
                    if adapter:
                        sent = bool(
                            await adapter.send_private_text_report(
                                delivery_target, text_report
                            )
                        )
                else:
                    if adapter:
                        if is_qq_official:
                            sent = bool(
                                await adapter.send_text_report(
                                    delivery_target.group_id,
                                    text_report,
                                    fallback_content=fallback_report,
                                )
                            )
                        else:
                            sent = bool(
                                await adapter.send_text_report(
                                    delivery_target.group_id, text_report
                                )
                            )
                    if not sent:
                        sent = bool(
                            await self.message_sender.send_text(
                                delivery_target.group_id,
                                f"📊 每日群聊分析报告：\n\n{text_report}",
                                delivery_target.platform_id,
                            )
                        )
            except Exception as e:
                logger.error(
                    f"[分发器] 发送文本报告最终失败。会话: {delivery_target.session_id}, 错误: {e}"
                )
                sent = False

            if dispatch_span:
                dispatch_span.setdefault("payload", {}).update(
                    {
                        "platform": delivery_target.platform_id or "auto",
                        "formats": ["text"],
                        "format": "text",
                        "success": bool(sent),
                        "text_sent": bool(sent),
                    }
                )
            return sent

    # ================================================================
    # 图片报告上传到群文件 / 群相册（仅 QQ 平台 image 格式）
    # ================================================================

    async def _try_upload_image(
        self,
        group_id: str,
        image_url: str,
        platform_id: str | None,
    ):
        """
        尝试将图片报告上传到群文件和/或群相册。

        仅在配置启用且平台为 OneBot 时执行，失败静默处理。
        """
        enable_file = self.config_manager.get_enable_group_file_upload()
        enable_album = self.config_manager.get_enable_group_album_upload()
        if not enable_file and not enable_album:
            return

        # 仅 OneBot 平台支持
        adapter = self._get_onebot_adapter(platform_id)
        if not adapter:
            return

        # 将图片保存为临时文件
        image_file = self._save_image_to_temp(image_url, group_id)
        if not image_file:
            return

        try:
            # 上传到群文件
            if enable_file:
                await self._do_upload_group_file(adapter, group_id, image_file)

            # 上传到群相册
            if enable_album:
                await self._do_upload_group_album(adapter, group_id, image_file)
        finally:
            try:
                os.remove(image_file)
            except OSError:
                pass

    async def _do_upload_group_file(
        self,
        adapter: GroupFileSupportProtocol | OneBotAdapter,
        group_id: str,
        file_path: str,
    ) -> None:
        """上传文件到群文件目录，失败静默"""
        try:
            folder_name = self.config_manager.get_group_file_folder()
            folder_id = None
            if folder_name:
                folder_id = await adapter.find_or_create_folder(group_id, folder_name)
            await adapter.upload_group_file_to_folder(
                group_id=group_id,
                file_path=file_path,
                folder_id=folder_id,
            )
        except Exception as e:
            logger.warning(f"群文件上传失败 (群 {group_id}): {e}")

    async def _do_upload_group_album(
        self,
        adapter: GroupAlbumSupportProtocol | OneBotAdapter,
        group_id: str,
        file_path: str,
    ) -> None:
        """上传图片到群相册，失败静默"""
        try:
            album_name = self.config_manager.get_group_album_name()
            strict_mode = self.config_manager.get_group_album_strict_mode()
            album_id = None

            if album_name:
                album_id = await adapter.find_album_id(group_id, album_name)
                if not album_id and strict_mode:
                    logger.info(
                        f"群相册严格模式开启：在群 {group_id} 中未找到名为 '{album_name}' 的相册，停止上传。"
                    )
                    return
            elif strict_mode:
                logger.info(
                    f"群相册严格模式开启：未设置目标相册名称，停止上传以防止操作群 {group_id} 的默认相册。"
                )
                return

            await adapter.upload_group_album(
                group_id,
                file_path,
                album_id=album_id,
                album_name=album_name,
                strict_mode=strict_mode,
            )
        except Exception as e:
            logger.warning(f"群相册上传失败 (群 {group_id}): {e}")

    def _save_image_to_temp(self, image_url: str, group_id: str) -> str | None:
        """将 base64 图片保存为临时 PNG 文件，返回路径。失败返回 None。"""
        try:
            image_data = None
            if image_url.startswith("base64://"):
                image_data = base64.b64decode(image_url[len("base64://") :])
            elif image_url.startswith("data:"):
                parts = image_url.split(",", 1)
                if len(parts) == 2:
                    image_data = base64.b64decode(parts[1])
            elif os.path.isfile(image_url):
                return os.path.abspath(image_url)
            elif image_url.startswith("file:///"):
                p = image_url[len("file:///") :]
                if os.path.isfile(p):
                    return os.path.abspath(p)

            if not image_data:
                return None

            date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(
                tempfile.gettempdir(), f"report_{group_id}_{date_str}.png"
            )
            with open(path, "wb") as f:
                f.write(image_data)
            return path
        except Exception as e:
            logger.debug(f"保存图片到临时文件失败: {e}")
            return None

    def _get_onebot_adapter(self, platform_id: str | None) -> OneBotAdapter | None:
        """获取 OneBot 适配器，非 OneBot 平台返回 None。"""
        if not platform_id:
            return None
        from ..platform.adapters.onebot_adapter import OneBotAdapter

        adapter = self.message_sender.bot_manager.get_adapter(platform_id)
        if isinstance(adapter, OneBotAdapter):
            return adapter
        return None
