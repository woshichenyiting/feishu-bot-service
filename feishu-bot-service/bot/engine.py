"""BotEngine — 基于 lark-oapi SDK v1.x 的飞书事件监听引擎。

设计要点：为什么 WS 跑在线程里，而不是子进程
------------------------------------------------
lark-oapi 的 ``ws.client`` 在 **模块导入时** 就抓了一个模块级全局 loop
(``loop = asyncio.get_event_loop()``)，``Client.start()`` 内部对这个全局 loop
做 ``run_until_complete``。所以它没法直接跑在 FastAPI 已经在运行的 loop 上。

旧实现为绕开这点起了 ``subprocess``，但子进程和主进程之间没有任何 IPC，拿不到
主进程里的 handler / llm_client —— 事件收到后无处可去，回调里只剩一行日志，
消息被直接丢弃。这正是"飞书后台投递 SUCCESS、大模型单测能通、但机器人从不
回复"的根因。

现在的做法：
    * WS client 跑在一个 daemon 线程里，线程建自己的 loop，并把 ``ws.client``
      的模块级 loop **重绑** 到它（该模块在主线程 import 时已绑过一次，不重绑
      会 run_until_complete 一个不属于本线程的 loop）。
    * SDK 要求事件回调是同步函数，因此回调里用
      ``asyncio.run_coroutine_threadsafe`` 把协程投回主 loop 执行。
    * 线程共享内存 → handler 直接可达，不需要 IPC，也不必把 app_secret 拼进
      子进程命令行（旧实现会让密钥出现在容器内 ``ps`` 输出里）。

数据流:
    飞书推送中心 ──WS──> [ws 线程] EventDispatcherHandler
                                      │ 同步回调 _on_message
                                      ↓ run_coroutine_threadsafe
                          [主 loop] MessageHandler.handle()
                                      ↓
                              LLM ──> 飞书 REST areply()
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from loguru import logger


class BotEngine:
    """飞书机器人事件监听引擎（WebSocket 长连接）。

    职责只有"传输层"：把飞书事件搬到主 loop 上交给 handler。
    调 LLM 和回消息都属于 :class:`bot.handler.MessageHandler`。
    """

    #: start() 等待连接建立的上限（秒）
    CONNECT_TIMEOUT = 15.0

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
        handler: Any | None = None,  # MessageHandler
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.encrypt_key = encrypt_key
        self.token = token
        # priority / events 由飞书开放平台后台控制订阅，SDK 不接这两个参数，
        # 这里仅保留用于日志，避免配置项看起来生效了其实没有。
        self.priority = priority
        self.events = events or ["im.message.receive_v1"]
        self.rate_limit = rate_limit or {}
        self.handler = handler

        self._ws_client: Any | None = None
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._ws_thread: threading.Thread | None = None
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._running = False

    # --------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """启动 WS 线程并等待连接建立。"""
        if not self.app_id or not self.app_secret:
            raise RuntimeError(
                "缺少飞书应用凭证：请检查 FEISHU_APP_ID / FEISHU_APP_SECRET"
            )

        self._main_loop = asyncio.get_running_loop()

        error_box: list[BaseException] = []
        self._ws_thread = threading.Thread(
            target=self._ws_thread_main,
            args=(error_box,),
            name="feishu-ws",
            daemon=True,
        )
        self._ws_thread.start()

        # 等连接真正建立。不能只等"线程起来了"就返回 —— 凭证错误时连接是在
        # client.start() 里才失败的，那样会报启动成功、真正的错误只躺在日志里。
        # SDK 连上后会给 _conn 赋值；连接异常会被线程放进 error_box。
        waited, step = 0.0, 0.1
        while waited < self.CONNECT_TIMEOUT:
            if error_box:
                raise RuntimeError(
                    f"飞书 WebSocket 连接失败: {error_box[0]}"
                ) from error_box[0]
            if not self._ws_thread.is_alive():
                raise RuntimeError("飞书 WebSocket 线程意外退出")
            if getattr(self._ws_client, "_conn", None) is not None:
                break
            await asyncio.sleep(step)
            waited += step
        else:
            raise RuntimeError(
                f"飞书 WebSocket 连接超时（{self.CONNECT_TIMEOUT:.0f}s）——"
                "请检查 app_id / app_secret 以及开放平台是否已开启长连接订阅"
            )

        self._running = True
        logger.info(
            "🚀 飞书 WebSocket 已启动（订阅事件由开放平台后台配置，当前配置声明: {}）",
            ", ".join(self.events),
        )

    async def stop(self) -> None:
        """断开 WebSocket。线程是 daemon，进程退出时自然回收。"""
        self._running = False
        client, ws_loop = self._ws_client, self._ws_loop
        if client is None or ws_loop is None:
            return

        # SDK 没有公开的 stop()（只有内部 _disconnect），先关掉自动重连再断开，
        # 否则断开后会立刻重连。
        try:
            client._auto_reconnect = False
            future = asyncio.run_coroutine_threadsafe(client._disconnect(), ws_loop)
            await asyncio.wait_for(asyncio.wrap_future(future), timeout=5)
            logger.info("已断开飞书 WebSocket 连接")
        except Exception as e:  # 关闭路径不该阻塞进程退出
            logger.warning("断开 WebSocket 时出错（忽略）: {}", e)

    async def __aenter__(self) -> "BotEngine":
        await self.start()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.stop()

    # ------------------------------------------------------------------ ws thread

    def _ws_thread_main(self, error_box: list[BaseException]) -> None:
        """WS 线程主体：建 loop → 建 client → 阻塞收消息。"""
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._ws_loop = loop

            import lark_oapi.ws.client as ws_client_module
            from lark_oapi.core.enum import LogLevel
            from lark_oapi.event.dispatcher_handler import EventDispatcherHandler

            # 关键：该模块 import 时已把模块级 loop 绑到主线程，这里重绑到本线程
            # 的 loop，否则 Client.start() 会 run_until_complete 一个不属于本线程
            # 的 loop。模块内 7 处 loop 用法全部引用这个全局，重绑即可全覆盖。
            ws_client_module.loop = loop

            event_handler = (
                EventDispatcherHandler.builder(
                    encrypt_key=self.encrypt_key,
                    verification_token=self.token,
                )
                .register_p2_im_message_receive_v1(self._on_message)
                .build()
            )

            self._ws_client = ws_client_module.Client(
                app_id=self.app_id,
                app_secret=self.app_secret,
                log_level=LogLevel.INFO,
                event_handler=event_handler,
            )

            self._ws_client.start()  # 阻塞，直到断开
        except BaseException as e:  # noqa: BLE001 — 必须捞干净，否则线程静默死掉
            logger.error("飞书 WebSocket 线程异常: {}", e)
            error_box.append(e)

    # ------------------------------------------------------------------ callback

    def _on_message(self, event: Any) -> None:
        """SDK 的同步事件回调（跑在 WS 线程）。

        这里只做一件事：把事件投回主 loop。任何耗时操作（LLM、HTTP）都不能在
        这个线程里做，否则会阻塞 WS 心跳。
        """
        if self.handler is None:
            logger.warning("未配置 handler，丢弃事件")
            return
        if self._main_loop is None:
            logger.warning("主 loop 未就绪，丢弃事件")
            return

        try:
            future = asyncio.run_coroutine_threadsafe(
                self._dispatch(event), self._main_loop
            )
        except Exception as e:
            logger.error("投递事件到主 loop 失败: {}", e)
            return

        def _log_error(f: Any) -> None:
            exc = f.exception()
            if exc is not None:
                logger.error("处理消息时抛出异常: {}", exc)

        # 不阻塞 WS 线程，只在出错时记日志
        future.add_done_callback(_log_error)

    async def _dispatch(self, event: Any) -> None:
        """在主 loop 上执行 handler。"""
        await self.handler.handle(event)
