"""总结投递的不可变配置与群目标。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ReportDeliveryMode = Literal[
    "disabled",
    "source_group",
    "forward_group",
    "serverchan",
    "forward_group_and_serverchan",
    "qq_official",
    "forward_group_and_qq_official",
    "qq_official_and_serverchan",
    "forward_group_and_qq_official_and_serverchan",
    "feishu",
    "forward_group_and_feishu",
    "qq_official_and_feishu",
    "serverchan_and_feishu",
    "forward_group_and_serverchan_and_feishu",
    "forward_group_and_qq_official_and_feishu",
    "qq_official_and_serverchan_and_feishu",
    "forward_group_and_qq_official_and_serverchan_and_feishu",
]

FORWARD_GROUP_DELIVERY_MODES: frozenset[ReportDeliveryMode] = frozenset(
    {
        "forward_group",
        "forward_group_and_serverchan",
        "forward_group_and_qq_official",
        "forward_group_and_qq_official_and_serverchan",
        "forward_group_and_feishu",
        "forward_group_and_serverchan_and_feishu",
        "forward_group_and_qq_official_and_feishu",
        "forward_group_and_qq_official_and_serverchan_and_feishu",
    }
)
QQ_OFFICIAL_DELIVERY_MODES: frozenset[ReportDeliveryMode] = frozenset(
    {
        "qq_official",
        "forward_group_and_qq_official",
        "qq_official_and_serverchan",
        "forward_group_and_qq_official_and_serverchan",
        "qq_official_and_feishu",
        "forward_group_and_qq_official_and_feishu",
        "qq_official_and_serverchan_and_feishu",
        "forward_group_and_qq_official_and_serverchan_and_feishu",
    }
)
SERVERCHAN_DELIVERY_MODES: frozenset[ReportDeliveryMode] = frozenset(
    {
        "serverchan",
        "forward_group_and_serverchan",
        "qq_official_and_serverchan",
        "forward_group_and_qq_official_and_serverchan",
        "serverchan_and_feishu",
        "forward_group_and_serverchan_and_feishu",
        "qq_official_and_serverchan_and_feishu",
        "forward_group_and_qq_official_and_serverchan_and_feishu",
    }
)
FEISHU_DELIVERY_MODES: frozenset[ReportDeliveryMode] = frozenset(
    {
        "feishu",
        "forward_group_and_feishu",
        "qq_official_and_feishu",
        "serverchan_and_feishu",
        "forward_group_and_serverchan_and_feishu",
        "forward_group_and_qq_official_and_feishu",
        "qq_official_and_serverchan_and_feishu",
        "forward_group_and_qq_official_and_serverchan_and_feishu",
    }
)
# 漫画投递优先级为指定群、QQ 官方、飞书，仅在未启用更高优先级群目标时发飞书。
FEISHU_COMIC_DELIVERY_MODES: frozenset[ReportDeliveryMode] = frozenset(
    FEISHU_DELIVERY_MODES - FORWARD_GROUP_DELIVERY_MODES - QQ_OFFICIAL_DELIVERY_MODES
)


@dataclass(frozen=True)
class GroupReportTarget:
    """报告发送目标，与用于生成和归档报告的源群分离。"""

    group_id: str
    platform_id: str | None

    @property
    def session_id(self) -> str:
        """获取实际投递的群会话标识。"""
        return self.group_id


@dataclass(frozen=True)
class QQOfficialPrivateTarget:
    """QQ 官方私聊目标，区分消息列表单聊与频道私聊。"""

    user_id: str
    platform_id: str
    scene: Literal["c2c", "guild"] = "c2c"
    source_guild_id: str = ""

    @property
    def session_id(self) -> str:
        """获取实际投递的私聊用户标识。"""
        return self.user_id


@dataclass(frozen=True)
class FeishuPrivateTarget:
    """飞书机器人私聊漫画目标，使用应用内用户标识。"""

    user_id: str
    user_id_type: str = "open_id"

    @property
    def session_id(self) -> str:
        """获取实际投递的私聊用户标识。"""
        return self.user_id


@dataclass(frozen=True)
class FeishuReuseTarget:
    """复用 AstrBot 已接入飞书(Lark)实例的私聊会话目标。"""

    user_id: str
    platform_id: str

    @property
    def session_id(self) -> str:
        """获取实际投递的私聊用户标识。"""
        return self.user_id

    @property
    def umo(self) -> str:
        """获取宿主统一会话标识（飞书私聊为 FriendMessage）。"""
        return f"{self.platform_id}:FriendMessage:{self.user_id}"


# 总结投递的群聊或私聊会话目标；飞书目标仅用于漫画私聊投递。
ChatReportTarget = GroupReportTarget | QQOfficialPrivateTarget
# 支持私聊发送的复用会话目标联合。
PrivateReportTarget = QQOfficialPrivateTarget | FeishuReuseTarget
ReportTarget = ChatReportTarget | FeishuPrivateTarget | FeishuReuseTarget


@dataclass(frozen=True)
class ServerChanBotConfig:
    """Server酱³ 机器人凭据和接收用户，令牌不参与对象展示。"""

    token: str = field(repr=False)
    chat_id: int
    silent: bool = False


@dataclass(frozen=True)
class FeishuBotConfig:
    """飞书自建应用机器人凭据和接收用户，密钥不参与对象展示。"""

    app_id: str
    app_secret: str = field(repr=False)
    user_id: str
    user_id_type: str = "open_id"
