"""对话上下文管理器 — 为每个会话维护滑动窗口消息历史。

会话键规则：
    私聊:      user_id (如 o_user_xxx)
    群聊:      group_id + ":" + user_id (如 og_group_xxx::o_user_xxx)
    每个会话最多保留 ``max_messages_per_user`` 条消息，超过则淘汰最早两条（user+assistant）。
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any


class ConversationContext:
    """轻量级异步安全的上下文管理器。"""

    def __init__(
        self,
        max_messages_per_user: int = 20,
        ttl_seconds: int = 0,
    ) -> None:
        self._max = max_messages_per_user
        self._ttl = ttl_seconds  # 0 = 永不过期
        # key -> [{"role": str, "content": str}, ...]
        self._stores: dict[str, list[dict[str, str]]] = defaultdict(list)
        # key -> last_access_time (epoch seconds)
        self._last_access: dict[str, float] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def build_key(group_id: str | None, user_id: str) -> str:
        """构建会话唯一标识。"""
        if group_id:
            return f"{group_id}::{user_id}"
        return user_id

    def get_messages(self, session_key: str) -> list[dict[str, str]]:
        """获取该会话的消息历史（不包含 system prompt）。"""
        return list(self._stores.get(session_key, []))

    async def add_message(
        self,
        session_key: str,
        role: str,
        content: str,
    ) -> None:
        """追加一条消息并管理滑动窗口。"""
        async with self._lock:
            history = self._stores[session_key]
            history.append({"role": role, "content": content})
            self._last_access[session_key] = time.time()

            # 滑动窗口：每次增加两条（user + assistant），所以阈值是 max * 2
            limit = self._max * 2
            while len(history) > limit:
                history.pop(0)
                history.pop(0)

    async def trim_to_limit(self, session_key: str) -> None:
        """如果超限，直接截断（用于初始化时调用）。"""
        async with self._lock:
            history = self._stores.get(session_key, [])
            limit = self._max * 2
            while len(history) > limit:
                history.pop(0)
                history.pop(0)

    async def cleanup_expired(self) -> int:
        """清理 TTL 已过期的会话，返回清理数量。"""
        if not self._ttl or self._ttl <= 0:
            return 0

        async with self._lock:
            now = time.time()
            expired_keys = [
                k for k, t in self._last_access.items() if now - t > self._ttl
            ]
            for k in expired_keys:
                del self._stores[k]
                del self._last_access[k]
            return len(expired_keys)
