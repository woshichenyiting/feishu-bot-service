"""bot/engine.py 的事件投递测试。

锁住旧实现最致命的那个 bug：WS 收到事件后没有任何路径能到达 handler。
旧实现把 WS 跑在 subprocess 里、回调只写了一行
``logger.info("WS event received")``，主进程的 handler 永远不会被调用 ——
飞书后台显示投递成功，但机器人从不回复。

现在 WS 跑在线程里，回调必须把事件通过 run_coroutine_threadsafe 投回主 loop。
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from bot.engine import BotEngine


class _RecordingHandler:
    def __init__(self) -> None:
        self.received: list[tuple[object, str]] = []

    async def handle(self, event: object) -> None:
        self.received.append((event, threading.current_thread().name))


@pytest.mark.asyncio
async def test_on_message_dispatches_from_ws_thread_to_main_loop():
    """从别的线程调 _on_message，handler 必须在主 loop 上被执行。"""
    handler = _RecordingHandler()
    engine = BotEngine(app_id="cli_x", app_secret="s", handler=handler)
    engine._main_loop = asyncio.get_running_loop()

    ws_thread_name = "fake-ws"
    fired = threading.Event()

    def _fake_ws_thread() -> None:
        engine._on_message("EVENT")   # SDK 就是这样同步回调的
        fired.set()

    t = threading.Thread(target=_fake_ws_thread, name=ws_thread_name)
    t.start()
    assert fired.wait(5), "_on_message 阻塞了 WS 线程"

    # 让主 loop 有机会执行被投递进来的协程
    for _ in range(100):
        if handler.received:
            break
        await asyncio.sleep(0.02)
    t.join(timeout=5)

    assert handler.received, "事件没有到达 handler —— 旧实现丢消息的 bug 复发了"
    event, ran_on = handler.received[0]
    assert event == "EVENT"
    assert ran_on != ws_thread_name, "handler 不能跑在 WS 线程上（会阻塞心跳）"


@pytest.mark.asyncio
async def test_on_message_without_handler_does_not_raise():
    """没配 handler 时只记日志，不能把 WS 线程搞崩。"""
    engine = BotEngine(app_id="cli_x", app_secret="s", handler=None)
    engine._main_loop = asyncio.get_running_loop()

    engine._on_message("EVENT")  # 不应抛异常


@pytest.mark.asyncio
async def test_start_without_credentials_fails_fast():
    """缺凭证要显式报错，而不是建出一个没有 token 的客户端静默失败。"""
    engine = BotEngine(app_id="", app_secret="", handler=_RecordingHandler())

    with pytest.raises(RuntimeError, match="缺少飞书应用凭证"):
        await engine.start()


@pytest.mark.asyncio
async def test_stop_is_safe_before_start():
    engine = BotEngine(app_id="cli_x", app_secret="s", handler=None)
    await engine.stop()  # 不应抛异常
