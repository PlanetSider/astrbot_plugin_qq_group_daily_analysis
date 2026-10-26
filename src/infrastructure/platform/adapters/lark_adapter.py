"""飞书(Lark)平台适配器防腐层：复用 AstrBot 已接入的飞书实例投递报告。

仅承担报告与漫画的发送职责，不提供历史消息拉取能力；被分析源群
仍需使用具备拉取能力的平台实例。
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from ....domain.value_objects.platform_capabilities import PlatformCapabilities
from ....domain.value_objects.report_delivery import FeishuReuseTarget
from ....utils.logger import logger
from ..base import PlatformAdapter

if TYPE_CHECKING:
    from collections.abc import Mapping

    from astrbot.api.event import MessageChain
    from astrbot.api.star import Context

    from ....domain.value_objects.report_delivery import PrivateReportTarget
    from ....domain.value_objects.unified_group import UnifiedGroup, UnifiedMember
    from ....domain.value_objects.unified_message import UnifiedMessage


class LarkAdapter(PlatformAdapter[object]):
    """通过宿主 Context.send_message 复用飞书实例的防腐层适配器。"""

    platform_name = "lark"

    def __init__(
        self,
        bot_instance: object,
        config: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(bot_instance, config)
        self._context: Context | None = None

    def set_context(self, context: Context | None) -> None:
        """保存宿主上下文，用于按统一会话标识发送消息。"""
        self._context = context
        super().set_context(context)

    def _init_capabilities(self) -> PlatformCapabilities:
        """声明飞书复用实例的投递能力：可发文本与图片，不承担历史拉取。"""
        return PlatformCapabilities(
            platform_name="lark",
            supports_message_history=False,
            supports_text_message=True,
            supports_image_message=True,
            supports_file_message=False,
            supports_group_info=False,
            supports_member_list=False,
            supports_user_avatar=False,
            supports_group_avatar=False,
        )

    async def fetch_messages(
        self,
        group_id: str,
        days: int = 1,
        max_count: int = 1000,
        before_id: str | None = None,
        since_ts: int | float | None = None,
    ) -> list[UnifiedMessage]:
        """飞书复用实例仅作投递目标，不支持历史消息拉取。"""
        return []

    def convert_to_raw_format(
        self, messages: list[UnifiedMessage]
    ) -> list[dict[str, object]]:
        """无历史拉取能力，统一消息按标准原始格式透传。"""
        return [
            {
                "message_id": msg.message_id,
                "time": msg.timestamp,
                "sender": {
                    "user_id": msg.sender_id,
                    "nickname": msg.sender_name,
                    "card": msg.sender_name,
                },
                "message": msg.text_content,
                "raw_message": msg.text_content,
                "group_id": msg.group_id,
            }
            for msg in messages
        ]

    async def _send_chain(self, umo: str, chain: MessageChain) -> bool:
        """按统一会话标识经宿主发送消息链，异常仅记录不中断。"""
        if not self._context:
            logger.error("[Lark] 未设置宿主 context，无法发送消息")
            return False
        try:
            return bool(await self._context.send_message(umo, chain))
        except Exception as exc:
            logger.error(f"[Lark] 消息发送失败（{umo}）：{type(exc).__name__}")
            return False

    def _build_image_chain(self, image: str, caption: str) -> MessageChain:
        """构造包含可选说明与图片的消息链，兼容四种图片来源。"""
        from astrbot.api.event import MessageChain

        chain = MessageChain()
        if caption:
            chain.message(caption)
        if image.startswith("base64://"):
            chain.base64_image(image[len("base64://") :])
        elif image.startswith("data:") and "," in image:
            chain.base64_image(image.split(",", 1)[1])
        elif image.startswith(("http://", "https://")):
            chain.url_image(image)
        else:
            chain.file_image(os.path.abspath(image))
        return chain

    async def send_text(
        self, group_id: str, text: str, reply_to: str | None = None
    ) -> bool:
        from astrbot.api.event import MessageChain

        umo = f"{self.platform_id}:GroupMessage:{group_id}"
        return await self._send_chain(umo, MessageChain().message(str(text)))

    async def send_image(
        self, group_id: str, image_path: str, caption: str = ""
    ) -> bool:
        umo = f"{self.platform_id}:GroupMessage:{group_id}"
        return await self._send_chain(umo, self._build_image_chain(image_path, caption))

    async def send_file(
        self,
        group_id: str,
        file_path: str,
        filename: str | None = None,
    ) -> bool:
        """飞书复用实例暂不支持文件上传，安全返回失败并回退文本。"""
        logger.warning("[Lark] 复用实例不支持发送文件，跳过文件投递")
        return False

    async def send_private_text(self, target: PrivateReportTarget, text: str) -> bool:
        from astrbot.api.event import MessageChain

        if not isinstance(target, FeishuReuseTarget):
            return False
        return await self._send_chain(target.umo, MessageChain().message(str(text)))

    async def send_private_text_report(
        self,
        target: PrivateReportTarget,
        content: str,
        fallback_content: str | None = None,
    ) -> bool:
        """分段发送私聊普通文本，部分失败时不重发已成功的片段。"""
        if not isinstance(target, FeishuReuseTarget):
            return False
        report = str(fallback_content or content or "").strip()
        if not report:
            return False
        max_chunk_size = 3800
        chunks: list[str] = []
        current = report
        while len(current) > max_chunk_size:
            split_idx = current.rfind("\n", 0, max_chunk_size)
            if split_idx == -1:
                split_idx = max_chunk_size
            chunks.append(current[:split_idx].strip())
            current = current[split_idx:].strip()
        if current:
            chunks.append(current)
        for chunk in chunks:
            if not await self.send_private_text(target, chunk):
                return False
        return True

    async def send_private_image(
        self, target: PrivateReportTarget, image: str, caption: str = ""
    ) -> bool:
        if not isinstance(target, FeishuReuseTarget):
            return False
        return await self._send_chain(
            target.umo, self._build_image_chain(image, caption)
        )

    async def get_group_info(self, group_id: str) -> UnifiedGroup | None:
        """复用实例不提供群元数据查询，来源群名由源群平台解析。"""
        return None

    async def get_group_list(self) -> list[str]:
        return []

    async def get_member_list(self, group_id: str) -> list[UnifiedMember]:
        return []

    async def get_member_info(
        self, group_id: str, user_id: str
    ) -> UnifiedMember | None:
        return None

    async def get_user_avatar_url(self, user_id: str, size: int = 100) -> str | None:
        return None

    async def get_user_avatar_data(self, user_id: str, size: int = 100) -> str | None:
        return None

    async def get_group_avatar_url(self, group_id: str, size: int = 100) -> str | None:
        return None

    async def batch_get_avatar_urls(
        self, user_ids: list[str], size: int = 100
    ) -> dict[str, str | None]:
        return {}
