"""MCPClient — 基于 SSE transport 的 MCP Client。

支持连接任意兼容 MCP 协议的 Server（通过 HTTP POST + SSE 事件流）。
用法示例:
    async with MCPClient(url="...") as client:
        tools = await client.list_tools()
        result = await client.call_tool("search_docs", {"query": "hello"})
"""

from __future__ import annotations

from typing import Any

from loguru import logger


class MCPTool:
    """表示一个 MCP Tool。"""

    def __init__(self, name: str, description: str, input_schema: dict[str, Any]) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }

    def __repr__(self) -> str:
        return f"MCPTool(name={self.name!r}, desc={self.description[:60]!r}...)"


class MCPClient:
    """MCP Client — 管理单个 MCP Server 的连接和通信（SSE transport）。"""

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.url = url
        self.headers = headers
        self.timeout = timeout
        self._session = None
        self._streams = None
        self._closed = True
        self._tools: list[MCPTool] = []

    @property
    def is_connected(self) -> bool:
        return not self._closed and self._session is not None

    async def connect(self) -> None:
        """建立 SSE 连接并完成 MCP 初始化握手。"""
        from mcp.client.sse import sse_client
        from mcp import ClientSession

        # 增加超时：SSE Server 首次响应可能较慢
        self._streams = sse_client(
            self.url,
            headers=self.headers,
            timeout=10.0,   # HTTP request timeout
            sse_read_timeout=120.0,  # SSE event stream read timeout
        )
        reader, writer = await self._streams.__aenter__()
        self._session = ClientSession(reader, writer)
        init_result = await self._session.initialize()

        server_info = init_result.server_info.implementation if init_result.server_info else None
        self._closed = False
        logger.info(
            "✅ MCP connected: %s v%s (%s)",
            server_info.name or "?",
            server_info.version or "?",
            self.url,
        )

    async def close(self) -> None:
        """关闭连接并释放资源。"""
        try:
            if self._session:
                await self._session.close()
        except Exception:
            pass
        try:
            if self._streams:
                await self._streams.__aexit__(None, None, None)
        except Exception:
            pass
        self._closed = True
        logger.debug("❌ MCP disconnected: %s", self.url)

    async def list_tools(self) -> list[MCPTool]:
        """列出当前 Server 可用的所有 Tools。"""
        if not self.is_connected:
            raise RuntimeError(f"MCP client not connected: {self.url}")

        result = await self._session.list_tools()
        tools = []
        for t in result.tools:
            tools.append(MCPTool(
                name=t.name,
                description=getattr(t, "description", ""),
                input_schema=getattr(t, "input_schema", {}),
            ))
        self._tools = tools
        logger.info("📦 MCP %s: %d tools available", self.url, len(tools))
        return tools

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """执行一个 Tool，返回结果字典。"""
        if not self.is_connected:
            raise RuntimeError(f"MCP client not connected: {self.url}")

        result = await self._session.call_tool(name, arguments or {})

        # Parse tool result content
        parts: list[str] = []
        for item in getattr(result, "content", []) or []:
            if hasattr(item, "text") and item.text:
                parts.append(str(item.text))
            elif isinstance(item, dict):
                parts.append(str(item))

        text = "\n\n".join(parts) if parts else ""
        errors = [getattr(e, "message", "") for e in getattr(result, "errors", []) or []]

        return {
            "tool_name": name,
            "result": text,
            "errors": errors,
            "ok": not errors,
        }
