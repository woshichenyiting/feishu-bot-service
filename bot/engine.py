"""BotEngine — 基于 lark-oapi SDK v1.x 的飞书事件监听引擎。

架构说明:
    ┌──────────────────────────────────────────────────────┐
    │              lark-oapi ws.client.Client               │
    │  (WebSocket 长连接 ←→ 飞书事件推送中心)                 │
    │                                                     │
    │  event_handler = EventDispatcherHandler.builder()     │
    │       .register_p2_im_message_receive_v1(callback)    │
    │       .build()                                        │
    │                                                     │
    │  Callback 接收: P2ImMessageReceiveV1                  │
    │    ├─ header.message_id  → 消息 ID（回复必需）           │
    │    ├─ header.sender      → Sender{user_id, chat_type} │
    │    └─ data.message       → Message                    │
    │         ├─ message_id                                 │
    │         ├─ chat_id / chat_type                        │
    │         ├─ msg_type                                   │
    │         ├─ body.content                               │
    │         └─ mentions[]                                 │
    └──────────────────────────────────────────────────────┘
    ┌──────────────────────────────────────────────────────┐
    │  BotEngine._http_client                              │
    │  ClientBuilder.app_id/secret.build()                  │
    │    └─ im.v1.message.areply(request)                   │
    │      → ReplyMessageRequest                          │
    │        └─ request_body(ReplyMessageRequestBody)      │
    │             .msg_type("text")                         │
    │             .content("<AI response>")                 │
    └──────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Awaitable, Callable

from loguru import logger


class BotEngine:
    """飞书机器人后端服务引擎。"""

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        encrypt_key: str = "",
        token: str = "",
        priority: int = 10,
        events: list[str] | None = None,
        rate_limit: dict[str, Any] | None = None,
        handler: Any | None = None,       # MessageHandler
        send_reply_fn: Any | None = None,  # Callable[[str], Awaitable[None]]
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.encrypt_key = encrypt_key
        self.token = token
        self.priority = priority
        self.events = events or ["im.message.receive_v1"]
        self.handler = handler
        self.rate_limit = rate_limit or {}
        self._send_reply_fn = send_reply_fn

        self._ws_client = None
        self._http_client = None
        self._running = False

    # --------------------------------------------------------------- lifecycle

    # --------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """启动: 建 HTTP 客户端 + 启动独立 WS 子进程。"""
        self._http_client = self._build_http_client()

        # WS Client 依赖模块级全局 loop，无法跨线程安全使用
        # 方案：启动一个独立子进程，该进程只运行 WS Client
        import subprocess
        script = f"""
import asyncio, sys, os
from loguru import logger as _log
_log.remove()
_log.add(sys.stderr, level="INFO")

sys.path.insert(0, {__file__.rsplit('/', 1)[0]!r})
from bot.engine import _ws_main
ws_engine = _ws_main({self.app_id!r}, {self.app_secret!r},
                      {self.encrypt_key!r}, {self.token!r})
asyncio.run(ws_engine)
"""
        env = dict(os.environ)
        env["PYTHONPATH"] = str(__file__.rsplit("/", 1)[0])
        self._ws_process = subprocess.Popen(
            [sys.executable, "-c", script],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        # Give the sub-process time to connect
        await asyncio.sleep(2)
        if self._ws_process.poll() is not None:
            err = self._ws_process.stderr.read().decode(errors="replace")
            logger.error("WS subprocess exited: %s", err[:500])
            raise RuntimeError(f"WS subprocess failed: {err}")

        self._running = True
        logger.info("🚀 飞书 WebSocket 子进程已启动，开始监听消息事件 ...")

    async def stop(self) -> None:
        """关闭 WebSocket 子进程。"""
        if getattr(self, "_ws_process", None):
            self._ws_process.terminate()
            try:
                self._ws_process.wait(timeout=3)
            except Exception:
                self._ws_process.kill()
            logger.info("已停止飞书 WebSocket 连接")
        self._running = False

    async def __aenter__(self) -> "BotEngine":
        await self.start()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.stop()


# Module-level entry point for WS subprocess (must not reference instance methods)
def _ws_run(app_id: str, app_secret: str, encrypt_key: str, token: str) -> None:  # noqa: D103
    """Run WebSocket client in its own process — called by subprocess."""
    import asyncio
    from lark_oapi.ws.client import Client as WSClient
    from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
    from lark_oapi.core.enum import LogLevel
    from loguru import logger

    eh_builder = EventDispatcherHandler.builder(
        encrypt_key=encrypt_key,
        verification_token=token,
    )

    eh_builder.register_p2_im_message_receive_v1(lambda v1_event: logger.info("WS event received"))
    eh = eh_builder.build()

    ws_client = WSClient(
        app_id=app_id,
        app_secret=app_secret,
        log_level=LogLevel.DEBUG,
        event_handler=eh,
    )

    logger.info("WS subprocess starting...")
    try:
        ws_client.start()
    except KeyboardInterrupt:
        pass
    finally:
        ws_client.stop()
        logger.info("WS subprocess stopped.")


# --------------------------------------------------------- BotEngine internals

class BotEngine:
    """飞书机器人后端服务引擎。"""

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        encrypt_key: str = "",
        token: str = "",
        priority: int = 10,
        events: list[str] | None = None,
        rate_limit: dict[str, Any] | None = None,
        handler: Any | None = None,       # MessageHandler
        send_reply_fn: Any | None = None,  # Callable[[str], Awaitable[None]]
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.encrypt_key = encrypt_key
        self.token = token
        self.priority = priority
        self.events = events or ["im.message.receive_v1"]
        self.handler = handler
        self.rate_limit = rate_limit or {}
        self._send_reply_fn = send_reply_fn

        self._http_client = None
        self._running = False
        self._ws_process = None

    async def start(self) -> None:
        """启动: 建 HTTP 客户端 + 启动独立 WS 子进程。"""
        self._http_client = self._build_http_client()

        import subprocess
        script = """
import asyncio, sys, os
from loguru import logger as _log
_log.remove()
_log.add(sys.stderr, level="INFO")

sys.path.insert(0, "%s")
from bot.engine import _ws_run
_ws_run("%s", "%s", "%s", "%s")
""" % (__file__.rsplit('/', 1)[0], self.app_id, self.app_secret, self.encrypt_key, self.token)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(__file__.rsplit("/", 1)[0])
        self._ws_process = subprocess.Popen(
            [sys.executable, "-c", script],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        await asyncio.sleep(2)
        if self._ws_process.poll() is not None:
            err = self._ws_process.stderr.read().decode(errors="replace")
            logger.error("WS subprocess exited: %s", err[:500])
            raise RuntimeError(f"WS subprocess failed: {err}")

        self._running = True
        logger.info("🚀 飞书 WebSocket 子进程已启动，开始监听消息事件 ...")

    async def stop(self) -> None:
        """关闭 WebSocket 子进程。"""
        if getattr(self, "_ws_process", None):
            self._ws_process.terminate()
            try:
                self._ws_process.wait(timeout=3)
            except Exception:
                self._ws_process.kill()
            logger.info("已停止飞书 WebSocket 连接")
        self._running = False

    @staticmethod
    def _build_http_client():
        """构建 HTTP 客户端（用于发消息）。"""
        from lark_oapi.client import ClientBuilder
        return ClientBuilder().app_id("").app_secret("").build()

    async def __aenter__(self) -> "BotEngine":
        await self.start()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.stop()

    async def _process_event(self, v1_event: Any) -> None:
        """处理解析后的 P2ImMessageReceiveV1 事件对象。"""
        from lark_oapi.api.im.v1.model import P2ImMessageReceiveV1Data

        data = v1_event.data if isinstance(v1_event, P2ImMessageReceiveV1Data) else v1_event
        msg = data.message

        logger.info(
            "收到飞书消息: msg_id={} chat_type={} lang={}",
            getattr(msg, "message_id", "?")[:8] if msg else "?",
            getattr(data, "chat_type", "unknown"),
            getattr(msg, "language", ""),
        )

        # 交给 handler 处理
        if self.handler:
            payload = {
                "header": {
                    "message_id": getattr(data.header, "message_id", None),
                },
                "event_data": v1_event,
            }
            await self.handler.handle(payload, self._make_send_reply())

    def _make_send_reply(self) -> Callable[[str], Awaitable[None]]:
        """返回闭包：将 AI 回复通过飞书 REST API 发送回去。"""
        async def _reply(text: str) -> None:
            if not self._send_reply_fn:
                logger.warning("未配置 send_reply_fn，跳过回复")
                return
            try:
                await self._send_reply_fn(text)
            except Exception as e:
                logger.error("发送回复失败: {}", e)

        return _reply
