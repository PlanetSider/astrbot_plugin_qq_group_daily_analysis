"""通过 Server酱³ 官方 Bot 接口推送总结。"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote

import aiohttp

from ...utils.logger import logger

if TYPE_CHECKING:
    from ...domain.value_objects.report_delivery import ServerChanBotConfig


class ServerChanBotSender:
    """仅使用 Bot Token 调用 sendMessage 的异步推送客户端。"""

    async def send_summary(self, config: ServerChanBotConfig, text: str) -> bool:
        """推送文本总结，HTTP 成功且接口明确返回 ok=true 才视为成功。

        Args:
            config: 经过校验的机器人令牌、UID 和静默开关。
            text: 待推送的文本总结。

        Returns:
            服务端明确确认接收时返回 True，否则返回 False。
        """
        if not text.strip():
            logger.error("[Server酱机器人] 总结内容为空，取消推送")
            return False
        url = f"https://bot-go.apijia.cn/bot{quote(config.token, safe='')}/sendMessage"
        payload = {
            "chat_id": config.chat_id,
            "text": text,
            "parse_mode": "markdown",
            "silent": config.silent,
        }
        try:
            async with (
                aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=30), trust_env=True
                ) as session,
                session.post(url, json=payload, allow_redirects=False) as response,
            ):
                if not 200 <= response.status < 300:
                    logger.error(
                        f"[Server酱机器人] 推送失败，HTTP 状态码：{response.status}"
                    )
                    return False
                body: object = await response.json()
                if not isinstance(body, dict) or body.get("ok") is not True:
                    logger.error(
                        "[Server酱机器人] 接口未确认推送成功，请检查令牌和 UID"
                    )
                    return False
        except Exception as exc:
            # 请求异常可能包含带令牌的 URL，仅记录异常类型，不输出凭据或响应正文。
            logger.error(f"[Server酱机器人] 推送异常：{type(exc).__name__}")
            return False
        logger.info("[Server酱机器人] 总结推送成功")
        return True
