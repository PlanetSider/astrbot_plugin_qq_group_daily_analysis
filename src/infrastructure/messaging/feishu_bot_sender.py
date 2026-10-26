"""通过飞书自建应用机器人私聊推送总结和漫画。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp

from ...utils.logger import logger

if TYPE_CHECKING:
    from ...domain.value_objects.report_delivery import FeishuBotConfig

_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
_IMAGE_UPLOAD_URL = "https://open.feishu.cn/open-apis/im/v1/images"
# 飞书错误描述可能较长，仅保留足够定位问题的摘要长度。
_MAX_ERROR_DETAIL_LENGTH = 200
_IMAGE_MAX_BYTES = 10 * 1024 * 1024


class FeishuBotSender:
    """使用应用凭证换取租户令牌并调用 im/v1 接口的异步推送客户端。"""

    async def send_summary(self, config: FeishuBotConfig, text: str) -> bool:
        """私聊推送文本总结，令牌获取和消息发送均确认成功才视为成功。

        Args:
            config: 经过校验的应用凭证和接收用户标识。
            text: 待推送的文本总结。

        Returns:
            飞书服务端明确确认接收时返回 True，否则返回 False。
        """
        if not text.strip():
            logger.error("[飞书机器人] 总结内容为空，取消推送")
            return False
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30), trust_env=True
            ) as session:
                token = await self._fetch_tenant_token(session, config)
                if token is None:
                    return False
                return await self._send_message(session, config, token, text)
        except Exception as exc:
            # 请求异常正文可能包含请求上下文，仅记录异常类型，不输出凭据或响应正文。
            logger.error(f"[飞书机器人] 推送异常：{type(exc).__name__}")
            return False

    async def _fetch_tenant_token(
        self,
        session: aiohttp.ClientSession,
        config: FeishuBotConfig,
    ) -> str | None:
        """获取租户访问凭证，未拿到有效令牌时返回 None。

        Args:
            session: 已配置超时的 aiohttp 会话。
            config: 经过校验的应用凭证和接收用户标识。

        Returns:
            有效的租户访问令牌，失败返回 None。
        """
        async with session.post(
            _TOKEN_URL,
            json={"app_id": config.app_id, "app_secret": config.app_secret},
        ) as response:
            if not 200 <= response.status < 300:
                logger.error(
                    f"[飞书机器人] 获取访问凭证失败，HTTP 状态码：{response.status}"
                )
                return None
            body: object = await response.json()
            if not isinstance(body, dict):
                logger.error("[飞书机器人] 获取访问凭证的响应不是有效对象")
                return None
            if body.get("code") != 0:
                logger.error(
                    "[飞书机器人] 获取访问凭证被拒绝，请检查 App ID 和 App Secret"
                )
                return None
            token: object = body.get("tenant_access_token")
            if not isinstance(token, str) or not token:
                logger.error("[飞书机器人] 访问凭证响应缺少有效令牌")
                return None
            return token

    async def _send_message(
        self,
        session: aiohttp.ClientSession,
        config: FeishuBotConfig,
        token: str,
        text: str,
    ) -> bool:
        """向指定用户发送私聊文本消息，服务端确认后才视为成功。

        Args:
            session: 已配置超时的 aiohttp 会话。
            config: 经过校验的应用凭证和接收用户标识。
            token: 租户访问令牌。
            text: 待推送的文本总结。

        Returns:
            飞书接口确认接收时返回 True，否则返回 False。
        """
        payload = {
            "receive_id": config.user_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        async with session.post(
            _MESSAGE_URL,
            params={"receive_id_type": config.user_id_type},
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            if not 200 <= response.status < 300:
                logger.error(f"[飞书机器人] 推送失败，HTTP 状态码：{response.status}")
                return False
            body: object = await response.json()
            if (
                not isinstance(body, dict)
                or body.get("code") != 0
                or not isinstance(body.get("data"), dict)
            ):
                # code 和 msg 是飞书官方错误描述，不含应用凭据，截断后记录便于排查。
                code: object = (
                    body.get("code") if isinstance(body, dict) else "无效响应"
                )
                detail = ""
                if isinstance(body, dict) and isinstance(body.get("msg"), str):
                    detail = body["msg"][:_MAX_ERROR_DETAIL_LENGTH]
                logger.error(
                    f"[飞书机器人] 接口未确认推送成功（code: {code}，msg: {detail}），"
                    "请检查用户 ID 与类型是否匹配、机器人权限及版本发布状态"
                )
                return False
        logger.info("[飞书机器人] 总结推送成功")
        return True

    async def send_comic(
        self,
        config: FeishuBotConfig,
        image_url: str,
        source_label: str = "",
    ) -> bool:
        """私聊推送漫画图片，上传成功且服务端确认后才视为成功。

        Args:
            config: 经过校验的应用凭证和接收用户标识。
            image_url: 漫画图片的本地文件路径或 HTTP(S) 地址。
            source_label: 附在图片上方的来源群标识，允许为空。

        Returns:
            飞书服务端明确确认接收时返回 True，否则返回 False。
        """
        image = await self._load_image_bytes(image_url)
        if image is None:
            return False
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60), trust_env=True
            ) as session:
                token = await self._fetch_tenant_token(session, config)
                if token is None:
                    return False
                image_key = await self._upload_image(session, config, token, image)
                if image_key is None:
                    return False
                return await self._send_image_message(
                    session, config, token, image_key, source_label
                )
        except Exception as exc:
            # 请求异常正文可能包含请求上下文，仅记录异常类型，不输出凭据或响应正文。
            logger.error(f"[飞书机器人] 漫画推送异常：{type(exc).__name__}")
            return False

    async def _load_image_bytes(self, image_url: str) -> bytes | None:
        """读取本地图片或下载远程图片，超限或失败时返回 None。

        Args:
            image_url: 图片的本地文件路径或 HTTP(S) 地址。

        Returns:
            图片二进制内容，失败返回 None。
        """
        try:
            if image_url.startswith(("http://", "https://")):
                async with (
                    aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=60), trust_env=True
                    ) as session,
                    session.get(image_url) as response,
                ):
                    if not 200 <= response.status < 300:
                        logger.error(
                            "[飞书机器人] 下载漫画图片失败，"
                            f"HTTP 状态码：{response.status}"
                        )
                        return None
                    image = await response.read()
            else:
                image = Path(image_url).read_bytes()
        except Exception as exc:
            logger.error(f"[飞书机器人] 读取漫画图片失败：{type(exc).__name__}")
            return None
        if not image:
            logger.error("[飞书机器人] 漫画图片内容为空，取消推送")
            return None
        if len(image) > _IMAGE_MAX_BYTES:
            logger.error("[飞书机器人] 漫画图片超过 10MB 上传限制，取消推送")
            return None
        return image

    async def _upload_image(
        self,
        session: aiohttp.ClientSession,
        config: FeishuBotConfig,
        token: str,
        image: bytes,
    ) -> str | None:
        """上传图片获取 image_key，未拿到有效 key 时返回 None。

        Args:
            session: 已配置超时的 aiohttp 会话。
            config: 经过校验的应用凭证和接收用户标识。
            token: 租户访问令牌。
            image: 图片二进制内容。

        Returns:
            有效的图片 image_key，失败返回 None。
        """
        form = aiohttp.FormData()
        form.add_field("image_type", "message")
        form.add_field("image", image, filename="comic.png", content_type="image/png")
        async with session.post(
            _IMAGE_UPLOAD_URL, data=form, headers={"Authorization": f"Bearer {token}"}
        ) as response:
            if not 200 <= response.status < 300:
                logger.error(
                    f"[飞书机器人] 上传漫画图片失败，HTTP 状态码：{response.status}"
                )
                return None
            body: object = await response.json()
            if not isinstance(body, dict) or body.get("code") != 0:
                logger.error("[飞书机器人] 上传漫画图片被拒绝，请检查 im:resource 权限")
                return None
            data: object = body.get("data")
            image_key: object = (
                data.get("image_key") if isinstance(data, dict) else None
            )
            if not isinstance(image_key, str) or not image_key:
                logger.error("[飞书机器人] 上传图片响应缺少有效 image_key")
                return None
            return image_key

    async def _send_image_message(
        self,
        session: aiohttp.ClientSession,
        config: FeishuBotConfig,
        token: str,
        image_key: str,
        source_label: str,
    ) -> bool:
        """以富文本图文消息发送漫画，服务端确认后才视为成功。

        Args:
            session: 已配置超时的 aiohttp 会话。
            config: 经过校验的应用凭证和接收用户标识。
            token: 租户访问令牌。
            image_key: 已上传图片的 image_key。
            source_label: 附在图片上方的来源群标识。

        Returns:
            飞书接口确认接收时返回 True，否则返回 False。
        """
        content = {
            "post": {
                "zh_cn": {
                    "title": "📊 每日群聊漫画",
                    "content": [
                        [
                            {
                                "tag": "text",
                                "text": f"{source_label}\n" if source_label else "",
                            }
                        ],
                        [{"tag": "img", "image_key": image_key}],
                    ],
                }
            }
        }
        payload = {
            "receive_id": config.user_id,
            "msg_type": "post",
            "content": json.dumps(content, ensure_ascii=False),
        }
        async with session.post(
            _MESSAGE_URL,
            params={"receive_id_type": config.user_id_type},
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            if not 200 <= response.status < 300:
                logger.error(
                    f"[飞书机器人] 漫画推送失败，HTTP 状态码：{response.status}"
                )
                return False
            body: object = await response.json()
            if (
                not isinstance(body, dict)
                or body.get("code") != 0
                or not isinstance(body.get("data"), dict)
            ):
                # code 和 msg 是飞书官方错误描述，不含应用凭据，截断后记录便于排查。
                code: object = (
                    body.get("code") if isinstance(body, dict) else "无效响应"
                )
                detail = ""
                if isinstance(body, dict) and isinstance(body.get("msg"), str):
                    detail = body["msg"][:_MAX_ERROR_DETAIL_LENGTH]
                logger.error(
                    f"[飞书机器人] 漫画接口未确认推送成功（code: {code}，msg: {detail}），"
                    "请检查用户 ID 与类型是否匹配、机器人权限及版本发布状态"
                )
                return False
        logger.info("[飞书机器人] 漫画推送成功")
        return True
