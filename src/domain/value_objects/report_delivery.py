"""总结投递的不可变配置与群目标。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ReportDeliveryMode = Literal["source_group", "forward_group", "serverchan"]


@dataclass(frozen=True)
class GroupReportTarget:
    """报告发送目标，与用于生成和归档报告的源群分离。"""

    group_id: str
    platform_id: str | None


@dataclass(frozen=True)
class ServerChanBotConfig:
    """Server酱³ 机器人凭据和接收用户，令牌不参与对象展示。"""

    token: str = field(repr=False)
    chat_id: int
    silent: bool = False
