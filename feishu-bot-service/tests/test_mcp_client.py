"""mcp_plugin/client.py 的 MCPClient.connect()/close() 测试。

锁住一个真实网络握手时才会暴露的 bug：官方 mcp SDK 的 ``ClientSession``
文档写明"enter as an async context manager, then call initialize()"——
``__aenter__`` 才会启动 dispatcher 的接收循环，跳过它直接调
``initialize()`` 会得到 ``JSONRPCDispatcher.send_raw_request called
before run()``。旧实现正是跳过了这一步，之前一直被更早的一个 bug（本地
``mcp/`` 包名和 pip 的 ``mcp`` SDK 冲突）挡在前面，包名冲突修好之后这个
才第一次被真实握手暴露出来。

用假的 ``ClientSession``/``sse_client`` 复现同样的顺序要求，不依赖真实
网络，这样任何人以后"重构简化"又漏掉 ``__aenter__`` 都会被测出来。
"""

from __future__ import annotations

import pytest

from mcp_plugin.client import MCPClient


class _FakeServerInfo:
    def __init__(self, name: str, version: str) -> None:
        self.name = name
        self.version = version


class _FakeInitResult:
    def __init__(self, server_info) -> None:
        self.server_info = server_info


class _FakeClientSession:
    """模拟官方 SDK 的顺序约束：initialize() 之前必须先 __aenter__()。"""

    def __init__(self, reader, writer) -> None:
        self.reader = reader
        self.writer = writer
        self.entered = False
        self.exited = False
        self.initialize_calls = 0
        self.list_tools_calls = 0

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc_info) -> None:
        self.exited = True

    async def initialize(self):
        if not self.entered:
            raise RuntimeError(
                "JSONRPCDispatcher.send_raw_request called before run()"
            )
        self.initialize_calls += 1
        return _FakeInitResult(_FakeServerInfo("fake-server", "1.0.0"))

    async def list_tools(self):
        if not self.entered:
            raise RuntimeError("not entered")
        self.list_tools_calls += 1

        class _Result:
            tools = []

        return _Result()

    # 故意不提供 close()，对应真实 SDK：ClientSession 没有 close()，
    # 只能靠 __aexit__ 关闭。如果实现代码手滑又调用 .close()，
    # AttributeError 会被 try/except 吞掉，__aexit__ 就不会被验证到。


class _FakeStreams:
    def __init__(self) -> None:
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return ("reader", "writer")

    async def __aexit__(self, *exc_info) -> None:
        self.exited = True


@pytest.fixture
def fake_sse(monkeypatch):
    """把 mcp_plugin/client.py 里延迟 import 的两个 SDK 符号换成假的。"""
    streams = _FakeStreams()
    sessions: list[_FakeClientSession] = []

    def _fake_sse_client(url, **kwargs):
        return streams

    def _fake_client_session(reader, writer):
        s = _FakeClientSession(reader, writer)
        sessions.append(s)
        return s

    monkeypatch.setattr("mcp.client.sse.sse_client", _fake_sse_client)
    monkeypatch.setattr("mcp.ClientSession", _fake_client_session)
    return streams, sessions


@pytest.mark.asyncio
async def test_connect_enters_session_before_initialize(fake_sse):
    """connect() 必须先 __aenter__ 会话，再调 initialize()——顺序反了
    会话就会抛 SDK 那句 "called before run()"。"""
    streams, sessions = fake_sse
    client = MCPClient(url="https://example.com/sse")

    await client.connect()

    assert client.is_connected
    session = sessions[0]
    assert session.entered, "connect() 必须先进入会话的 async context"
    assert session.initialize_calls == 1


@pytest.mark.asyncio
async def test_close_uses_aexit_not_nonexistent_close(fake_sse):
    """close() 必须用 __aexit__ 收尾；SDK 的 ClientSession 没有 close()。"""
    streams, sessions = fake_sse
    client = MCPClient(url="https://example.com/sse")
    await client.connect()

    await client.close()

    assert sessions[0].exited, "session 没有被正确 __aexit__，关闭逻辑没生效"
    assert streams.exited
    assert not client.is_connected


@pytest.mark.asyncio
async def test_list_tools_requires_prior_connect(fake_sse):
    client = MCPClient(url="https://example.com/sse")
    with pytest.raises(RuntimeError, match="not connected"):
        await client.list_tools()
