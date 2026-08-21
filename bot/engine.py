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

    async def start(self) -> None:
        """启动: 建 HTTP 客户端 + WebSocket 监听器（后台线程）。"""
        self._http_client = self._build_http_client()
        
        # WS Client 必须在新的事件循环中运行（不能在 run_in_executor 里嵌套）
        ws_ready = asyncio.Event()
        ws_error: list[Exception] = []
        
        def _ws_thread():
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            try:
                from lark_oapi.ws.client import Client as WSClient
                from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
                from lark_oapi.core.enum import LogLevel
        
                eh_builder = EventDispatcherHandler.builder(
                    encrypt_key=self.encrypt_key,
                    verification_token=self.token,
                )
        
                def _on_im_msg_recv(v1_event: Any) -> None:
                    loop = asyncio.get_running_loop()
                    asyncio.ensure_future(self._process_event(v1_event), loop=loop)
        
                eh_builder.register_p2_im_message_receive_v1(_on_im_msg_recv)
                eh = eh_builder.build()
        
                self._ws_client = WSClient(
                    app_id=self.app_id,
                    app_secret=self.app_secret,
                    log_level=LogLevel.DEBUG,
                    event_handler=eh,
                )
        
                # 连接成功或失败后都标记 ready
                async def _wrapped_start():
                    try:
                        await self._ws_client.start()
                    except Exception as e:
                        ws_error.append(e)
                    finally:
                        ws_ready.set()
                
                new_loop.create_task(_wrapped_start())
                # 跑 forever 直到有人停止
                new_loop.run_forever()
            except Exception as e:
                ws_error.append(e)
                ws_ready.set()
            finally:
                ws_ready.set()
        
        import threading
        t = threading.Thread(target=_ws_thread, daemon=True, name="feishu-ws")
        t.start()
        
        await asyncio.wait_for(ws_ready.wait(), timeout=5.0)
        self._running = True
        logger.info("🚀 飞书 WebSocket 已连接，开始监听消息事件 ...")

    async def stop(self) -> None:
        """关闭 WebSocket。"""
        if self._ws_client and self._running:
            try:
                await self._ws_client.stop()
            except Exception:
                pass
        self._running = False
        logger.info("已停止飞书 WebSocket 连接")

    async def __aenter__(self) -> "BotEngine":
        await self.start()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.stop()

    # --------------------------------------------------------- internals

    @staticmethod
    def _build_http_client():
        """构建 HTTP 客户端（用于发消息）。"""
        from lark_oapi.client import ClientBuilder
        return ClientBuilder().app_id("").app_secret("").build()

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
