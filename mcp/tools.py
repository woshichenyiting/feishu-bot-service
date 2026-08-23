"""Tool Router — 基于关键词匹配的工具路由。

根据用户消息的语义，匹配最合适的 MCP Tool 进行调用。
使用简单的 TF-IDF 近似（词频匹配），不引入额外依赖。
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from loguru import logger

from mcp.client import MCPTool


class ToolRouter:
    """通过关键词匹配将用户意图路由到 MCP Tool。"""

    def __init__(self, tools: list[MCPTool]) -> None:
        self.tools = tools
        self._index = self._build_index(tools)

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        """分词：保留中文字符和英文单词。"""
        chinese = re.findall(r"[\u4e00-\u9fff]", text)
        english = re.findall(r"[a-zA-Z]+", text.lower())
        return set(chinese) | set(english)

    @staticmethod
    def _build_index(tools: list[MCPTool]) -> dict[str, list[tuple[int, str]]]:
        """构建索引：关键词 → [(tool_index, description)]。"""
        index: dict[str, list[tuple[int, str]]] = {}
        for idx, tool in enumerate(tools):
            desc_words = ToolRouter._tokenize(tool.description)
            name_words = ToolRouter._tokenize(tool.name)
            words = desc_words | name_words
            for w in words:
                index.setdefault(w, []).append((idx, tool.name))
        return index

    def route(self, query: str, top_k: int = 3) -> list[tuple[int, MCPTool]]:
        """匹配用户查询，返回得分最高的 Top-K Tool。"""
        if not self.tools or not self._index:
            return []

        query_tokens = self._tokenize(query)
        scores: dict[int, float] = {}

        for token in query_tokens:
            if token in self._index:
                for idx, name in self._index[token]:
                    scores[idx] = scores.get(idx, 0) + 1

        # Sort by score descending
        sorted_tools = sorted(scores.items(), key=lambda x: -x[1])[:top_k]
        result = [(idx, self.tools[idx]) for idx, _ in sorted_tools]
        if result:
            logger.debug("🎯 Tool routing: query=%r matched %d tools", query[:30], len(result))
        return result
