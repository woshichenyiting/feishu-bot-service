"""LLM Client 抽象接口 + OpenAI 兼容实现。

支持任意 OpenAI 兼容格式的 API，包括：
    - OpenAI (gpt-4, gpt-3.5-turbo)
    - 通义千问 (dashscope)
    - 智谱 (bigmodel)
    - DeepSeek
    - 本地 Ollama / vLLM 等
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

from loguru import logger
from openai import AsyncOpenAI


class LLMClient(ABC):
    """LLM Client abstract base class."""

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        stream: bool = False,
    ) -> str:
        """Send a conversation turn and return model response text."""

    @abstractmethod
    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        on_token: callable[[str], None],
    ) -> str:
        """流式调用，每次收到 token 时回调 ``on_token(token)``。

        Returns:
            累计的完整文本。
        """


class OpenAIClient(LLMClient):
    """基于 ``openai.AsyncOpenAI`` 的通用客户端。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str = "gpt-4o-mini",
        temperature: float = 0.7,
        max_tokens: int = 2048,
        top_p: float = 0.9,
        system_prompt: str | None = None,
    ) -> None:
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._top_p = top_p
        self._system_prompt = system_prompt or ""

        if self._system_prompt:
            logger.info(
                "LLM 已初始化: provider=openai-compatible model={} base_url={}",
                self._model,
                base_url,
            )

    # ---- abstract implementations ----

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        combined = self._build_messages(messages)
        resp = await self._client.chat.completions.create(
            model=self._model,
            messages=combined,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            top_p=self._top_p,
            **kwargs,
        )
        text = resp.choices[0].message.content or ""
        logger.debug("LLM response ({:.1f} chars)", len(text))
        return text

    async def chat_stream(
        self, messages: list[dict[str, str]], on_token: callable[[str], None]
    ) -> str:
        combined = self._build_messages(messages)
        chunks: list[str] = []

        async for chunk in await self._client.chat.completions.create(
            model=self._model,
            messages=combined,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            top_p=self._top_p,
            stream=True,
        ):
            delta = chunk.choices[0].delta.content or ""
            if delta:
                on_token(delta)
                chunks.append(delta)

        full = "".join(chunks)
        logger.debug("LLM stream response ({:.1f} chars)", len(full))
        return full

    def _build_messages(self, messages: list[dict[str, str]]) -> list[dict[str, str]]:
        """在消息列表头部插入 system prompt（如果未存在）。"""
        result: list[dict[str, str]] = []
        if self._system_prompt:
            # 只在第一个 user/assistant 之前插入
            has_system = any(m.get("role") == "system" for m in messages)
            if not has_system:
                result.append({"role": "system", "content": self._system_prompt})
        result.extend(messages)
        return result
