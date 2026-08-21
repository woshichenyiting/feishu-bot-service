"""消息处理器 — 过滤 @占位符、路由群聊/私聊，驱动 LLM 并回复飞书。"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Awaitable, Callable

from loguru import logger
from lark_oapi.api.im.v1.model import (
    P2ImMessageReceiveV1Data,
    SenderBuilder,
)

from llm.client import LLMClient
from bot.context import ConversationContext


class MessageHandler:
    """核心消息处理管道。

    流程:
        1. 检查是否满足触发条件（trigger_mode + response_scope）
        2. 过滤消息长度
        3. 群聊中去除 @占位符文本
        4. 追加到对话上下文
        5. 调用 LLM 获取回复
        6. 将回复发送到飞书
    """

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        send_reply_fn: Callable[[str], Awaitable[None]],
        trigger_mode: str = "at_or_direct",   # at_or_direct | only_at
        response_scope: str = "group_and_dm",  # group_and_dm | group_only | dm_only
        strip_at_placeholder: bool = True,
        max_message_length: int = 5000,
        max_reply_length: int = 4000,
        max_messages_per_user: int = 20,
        ttl_seconds: int = 0,
    ) -> None:
        self._llm = llm_client
        self.send_reply_fn = send_reply_fn
        self.trigger_mode = trigger_mode
        self.response_scope = response_scope
        self.strip_at_placeholder = strip_at_placeholder
        self.max_message_length = max_message_length
        self.max_reply_length = max_reply_length
        self._context = ConversationContext(
            max_messages_per_user=max_messages_per_user,
            ttl_seconds=ttl_seconds,
        )

        # 简易 TokenBucket
        self._user_buckets: dict[str, list[float]] = {}
        self._global_times: list[float] = []
        self._lock = asyncio.Lock()

    # --------------------------------------------------------------- public

    async def handle(self, payload: dict, _unused: Any) -> None:
        """处理一条飞书消息事件。"""
        v1_event = payload.get("event_data")
        if not v1_event or not isinstance(v1_event, P2ImMessageReceiveV1Data):
            return

        msg = v1_event.data.message
        header = v1_event.header
        sender_info = v1_event.data.sender

        message_type = getattr(msg, "msg_type", "")
        chat_type = getattr(msg, "chat_type", "") or getattr(
            v1_event.data, "chat_type", ""
        )
        sender_type = getattr(sender_info, "sender_id", {}).get("member_type", "")
        user_id = (getattr(sender_info, "sender_id", {}) or {}).get(
            "open_id", "unknown"
        )
        room_id = getattr(msg, "chat_id", "") if chat_type == "group" else ""
        message_id = getattr(msg, "message_id", "")
        text_raw = self._extract_text(msg)

        # 0. 忽略其他机器人的消息
        if sender_type == "app":
            return

        # 1. 只处理文本消息
        if message_type != "text":
            logger.debug("非文本消息 %s, 跳过", message_type)
            return

        # 2. 检查响应范围
        if not self._check_scope(chat_type):
            logger.debug("不在响应范围内: scope=%s chat_type=%s", self.response_scope, chat_type)
            return

        # 3. 检查触发模式
        if not self._check_trigger(text_raw, chat_type, user_id, v1_event):
            return

        # 4. 过滤长度
        if len(text_raw) > self.max_message_length:
            logger.warning("消息超长 (%d), 跳过", len(text_raw))
            return

        # 5. 速率限制
        if not await self._check_rate_limit(user_id):
            logger.warning("用户 %s... 超出速率限制，跳过", user_id[:8])
            return

        session_key = ConversationContext.build_key(room_id, user_id)

        # 6. 清洗文本（去 @占位符）
        clean_text = self._clean_message(text_raw)
        if not clean_text:
            logger.info("清理后消息为空，跳过")
            return

        logger.info(
            "处理消息: user={} chat={} msg={} len={}",
            user_id[:8],
            room_id[:8] if room_id else "p2p",
            message_id[:8] if message_id else "?",
            len(clean_text),
        )

        # 7. 构建历史消息列表 + 追加 user 消息
        history = self._context.get_messages(session_key)
        await self._context.add_message(session_key, "user", clean_text)

        # 8. 调用 LLM
        try:
            reply = await self._llm.chat(history + [{"role": "user", "content": clean_text}])
        except Exception as e:
            logger.error("LLM 调用失败: {}", e)
            reply = "抱歉，服务暂时不可用，请稍后再试。"

        # 9. 截断回复
        reply = (reply or "")[: self.max_reply_length]
        if not reply:
            reply = "（无回复内容）"

        # 10. assistant 消息加入历史
        await self._context.add_message(session_key, "assistant", reply)

        # 11. 发送回复
        try:
            await self.send_reply_fn(reply, message_id, chat_type, user_id)
        except Exception as e:
            logger.error("发送回复失败: {}", e)

    # ------------------------------------------------------------------ private

    @staticmethod
    def _extract_text(msg: Any) -> str:
        """从 Message 对象提取纯文本。"""
        body = getattr(msg, "body", None)
        if body:
            content = getattr(body, "content", "")
            if content:
                return str(content)
        # Fallback: check mentions
        for m in getattr(msg, "mentions", []) or []:
            pass  # mentions are handled by _clean_message
        return ""

    def _check_scope(self, chat_type: str) -> bool:
        if chat_type == "group":
            return self.response_scope in ("group_and_dm", "group_only")
        return self.response_scope in ("group_and_dm", "dm_only")

    def _check_trigger(
        self, text: str, chat_type: str, user_id: str, event: Any
    ) -> bool:
        if self.trigger_mode == "at_or_direct":
            return True
        if chat_type == "group" and self.trigger_mode == "only_at":
            sender_type = (
                getattr(event.data.sender, "sender_id", {}) or {}
            ).get("member_type", "")
            if sender_type == "app":
                return False
            return "@" in text
        return True

    def _clean_message(self, text: str) -> str:
        """去除群聊中的 @占位符。"""
        if not self.strip_at_placeholder:
            return text
        # 飞书 @占位符格式：@_user_xxx or @_mention_yyy (支持中文用户名)
        cleaned = re.sub(r"@[_a-zA-Z0-9\u4e00-\u9fff\u3400-\u4dbf]+(?:\([^)]*\))?", "", text).strip()
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        return cleaned

    async def _check_rate_limit(self, user_id: str) -> bool:
        async with self._lock:
            now = asyncio.get_event_loop().time()
            limit_global = self.rate_limit.get("global_per_second", 10)
            limit_user = self.rate_limit.get("per_user_per_second", 3)

            self._global_times = [t for t in self._global_times if now - t < 1.0]
            if len(self._global_times) >= limit_global:
                return False
            self._global_times.append(now)

            bucket = self._user_buckets.setdefault(user_id, [])
            bucket[:] = [t for t in bucket if now - t < 1.0]
            if len(bucket) >= limit_user:
                return False
            bucket.append(now)
            return True
