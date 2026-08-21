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

    def to_openai_schema(self) -> dict[str, Any]:
        """转成 OpenAI 兼容 API 的 function-calling 工具描述。

        MCP 的 ``inputSchema`` 本来就是标准 JSON Schema，直接透传到
        ``parameters`` 即可，不需要额外转换。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description or "",
                "parameters": self.input_schema or {"type": "object", "properties": {}},
            },
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
        # 这两个 import 指的是 pip 装的官方 mcp SDK，不是本包（mcp_plugin）。
        # 本包以前就叫 mcp/，跟 pip 包同名，import 时会互相 shadow —— 这就是
        # 之前"配好 MCP server 却连不上"的根因，改名后这两行才能正确解析。
        from mcp.client.sse import sse_client
        from mcp import ClientSession

        self._streams = sse_client(
            self.url,
            headers=self.headers,
            timeout=self.timeout,
            sse_read_timeout=30.0,
        )
        reader, writer = await self._streams.__aenter__()
        self._session = ClientSession(reader, writer)
        # ClientSession's docstring is explicit: "enter as an async context
        # manager, then call initialize()". __aenter__ is what starts the
        # dispatcher's receive loop in a task group — skipping it makes any
        # request (including initialize()) fail with
        # "JSONRPCDispatcher.send_raw_request called before run()".
        await self._session.__aenter__()
        init_result = await self._session.initialize()

        # server_info IS the Implementation object (name/version/...), not a
        # wrapper with a further .implementation attribute.
        server_info = init_result.server_info
        self._closed = False
        logger.info(
            "✅ MCP connected: {} v{} ({})",
            server_info.name if server_info else "?",
            server_info.version if server_info else "?",
            self.url,
        )

    async def close(self) -> None:
        """关闭连接并释放资源。"""
        try:
            # ClientSession has no close(); __aexit__ is what stops the
            # dispatcher's task group.
            if self._session:
                await self._session.__aexit__(None, None, None)
        except Exception:
            pass
        try:
            if self._streams:
                await self._streams.__aexit__(None, None, None)
        except Exception:
            pass
        self._closed = True
        logger.debug("❌ MCP disconnected: {}", self.url)

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
        logger.info("📦 MCP {}: {} tools available", self.url, len(tools))
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
