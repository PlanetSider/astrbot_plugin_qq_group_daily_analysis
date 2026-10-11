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
]

FORWARD_GROUP_DELIVERY_MODES: frozenset[ReportDeliveryMode] = frozenset(
    {
        "forward_group",
        "forward_group_and_serverchan",
        "forward_group_and_qq_official",
        "forward_group_and_qq_official_and_serverchan",
    }
)
QQ_OFFICIAL_DELIVERY_MODES: frozenset[ReportDeliveryMode] = frozenset(
    {
        "qq_official",
        "forward_group_and_qq_official",
        "qq_official_and_serverchan",
        "forward_group_and_qq_official_and_serverchan",
    }
)
SERVERCHAN_DELIVERY_MODES: frozenset[ReportDeliveryMode] = frozenset(
    {
        "serverchan",
        "forward_group_and_serverchan",
        "qq_official_and_serverchan",
        "forward_group_and_qq_official_and_serverchan",
    }
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


ReportTarget = GroupReportTarget | QQOfficialPrivateTarget


@dataclass(frozen=True)
class ServerChanBotConfig:
    """Server酱³ 机器人凭据和接收用户，令牌不参与对象展示。"""

    token: str = field(repr=False)
    chat_id: int
    silent: bool = False
