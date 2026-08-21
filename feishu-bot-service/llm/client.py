"""LLM Client 抽象接口 + OpenAI 兼容实现。

支持任意 OpenAI 兼容格式的 API，包括：
    - OpenAI (gpt-4, gpt-3.5-turbo)
    - 通义千问 (dashscope)
    - 智谱 (bigmodel)
    - DeepSeek
    - 本地 Ollama / vLLM 等
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

from loguru import logger
from openai import AsyncOpenAI


@dataclass
class ToolCallRequest:
    """模型要求执行的一次工具调用（对应 OpenAI 的 tool_calls[i]）。"""

    id: str
    name: str
    arguments: str  # 原始 JSON 字符串，由调用方自行解析


@dataclass
class ChatResult:
    """带 function calling 结果的一次模型回复。

    ``tool_calls`` 为空列表时表示模型给出了最终文本回复；非空时调用方
    需要执行这些工具、把结果以 ``role: tool`` 消息追加回去，再调一轮。
    """

    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)


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
    async def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> ChatResult:
        """Send a conversation turn with function-calling tools offered.

        Returns the raw ``content``/``tool_calls`` split so the caller can
        run the execute-and-feed-back loop itself.
        """

    @abstractmethod
    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        on_token: Callable[[str], None],
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

    async def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> ChatResult:
        combined = self._build_messages(messages)
        resp = await self._client.chat.completions.create(
            model=self._model,
            messages=combined,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            top_p=self._top_p,
            # tools=[] 在部分 OpenAI 兼容网关上会被当成"禁止工具"处理，
            # 跟不传是两回事；候选为空时统一传 None。
            tools=tools or None,
        )
        msg = resp.choices[0].message
        calls = [
            ToolCallRequest(id=tc.id, name=tc.function.name, arguments=tc.function.arguments)
            for tc in (msg.tool_calls or [])
        ]
        logger.debug(
            "LLM tool-call turn: {} tool_calls, content={:.1f} chars",
            len(calls),
            len(msg.content or ""),
        )
        return ChatResult(content=msg.content, tool_calls=calls)

    async def chat_stream(
        self, messages: list[dict[str, str]], on_token: Callable[[str], None]
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
