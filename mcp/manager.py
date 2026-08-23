"""MCPManager — 管理多个 MCP Server 的连接、Tool 发现和执行。"""

from __future__ import annotations

from typing import Any

from loguru import logger

from mcp.client import MCPClient, MCPTool


class MCPManager:
    """管理多个 MCP Server，统一列出/调用 Tool。"""

    def __init__(self, servers_cfg: list[dict[str, Any]]) -> None:
        self._clients: dict[str, MCPClient] = {}
        self._all_tools: list[MCPTool] = []  # (source, tool) pairs

        for srv in servers_cfg:
            name = srv.get("name", srv["url"])
            client = MCPClient(
                url=srv["url"],
                headers=srv.get("headers"),
                timeout=srv.get("timeout", 5.0),
            )
            self._clients[name] = client

    @property
    def is_connected(self) -> bool:
        return all(c.is_connected for c in self._clients.values())

    async def connect(self) -> int:
        """连接所有 Server，返回成功数量。"""
        for name, client in self._clients.items():
            try:
                await client.connect()
                tools = await client.list_tools()
                self._all_tools.extend(tools)
            except Exception as e:
                logger.warning("⚠️  Failed to connect MCP %s: %s", name, e)
        total = len(self._all_tools)
        logger.info("🎯 MCP Manager: connected %d/%d servers, %d total tools",
                     len([c for c in self._clients.values() if c.is_connected]),
                     len(self._clients), total)
        return total

    async def close(self) -> None:
        """关闭所有连接。"""
        for client in self._clients.values():
            await client.close()

    def list_all_tools(self) -> list[MCPTool]:
        """列出所有可用 Tools。"""
        return list(self._all_tools)

    async def call_tool_by_name(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """通过工具名查找并调用（按第一个匹配到的 Server）。"""
        for tool in self._all_tools:
            if tool.name == name:
                for client in self._clients.values():
                    if any(t.name == name for t in client._tools):
                        return await client.call_tool(name, arguments)
        raise KeyError(f"Tool not found: {name}")
