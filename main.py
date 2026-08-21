"""飞书机器人后端服务 — 入口点。

架构:
    1. lark-oapi WebSocket → 接收 im.message.receive_v1 事件
    2. handler.py           → 解析、过滤 @占位符、路由群聊/私聊
    3. llm/client.py        → 调用 OpenAI 兼容大模型 API
    4. 飞书 REST API         → areply() / acreate() 回复消息

用法:
    # 1. 配置
    cp config.yaml config.local.yaml   # 修改你的凭证
    cp .env.example .env               # 填入真实值

    # 2. 开发运行
    source .venv/bin/activate
    python main.py

    # 3. Docker 运行
    docker compose up --build -d
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Awaitable

# ── 环境变量 ──────────────────────────────────────────────────────
from dotenv import load_dotenv

load_dotenv()

# ── 日志 ─────────────────────────────────────────────────────────
from loguru import logger

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# ── 配置加载 ──────────────────────────────────────────────────────
from utils.config import get_config

def setup_logging() -> None:
    cfg = get_config()
    level = cfg.get("logging", {}).get("level", LOG_LEVEL)
    fmt = cfg.get("logging", {}).get(
        "format", "{time:YYYY-MM-DD HH:mm:ss} | {level} | {name} | {message}"
    )
    logger.remove()
    logger.add(sys.stderr, level=level.upper(), format=fmt)


# ── FastAPI 应用（健康检查等） ───────────────────────────────────────
from fastapi import FastAPI

app = FastAPI(
    title="Feishu Bot Service",
    description="飞书机器人后端 — WebSocket 监听 + LLM 对话回传",
    version="0.1.0",
)


@app.on_event("startup")
async def startup_event() -> None:
    """启动时初始化所有组件。"""
    setup_logging()
    logger.info("=== 飞书机器人后端服务启动 ===")

    cfg = get_config()

    # ── 1. HTTP Client (用于发消息) ─────────────────────────────
    from lark_oapi.client import ClientBuilder
    http_client = ClientBuilder().build()

    # ── 2. LLM Client ──────────────────────────────────────────
    llm_cfg = cfg["llm"]
    from llm.client import OpenAIClient
    llm_client = OpenAIClient(
        base_url=llm_cfg["base_url"],
        api_key=llm_cfg["api_key"],
        model=llm_cfg.get("model", "qwen-plus"),
        temperature=llm_cfg.get("temperature", 0.7),
        max_tokens=llm_cfg.get("max_tokens", 2048),
        top_p=llm_cfg.get("top_p", 0.9),
        system_prompt=llm_cfg.get("system_prompt", ""),
    )

    # ── 3. [新增] MCP Manager（可选） ─────────────────────────────
    mcp_manager = None
    mcp_cfg = cfg.get("mcp", {})
    if mcp_cfg.get("enabled") and mcp_cfg.get("servers"):
        from mcp.manager import MCPManager
        mcp_manager = MCPManager(mcp_cfg["servers"])
        n_tools = await mcp_manager.connect()
        logger.info("🔌 MCP initialized: %d tools loaded", n_tools)

    # ── 4. Handler ─────────────────────────────────────────────
    bot_cfg = cfg.get("bot", {})
    ctx_cfg = cfg.get("context", {})
    rl_cfg = cfg.get("rate_limit", {})

    async def send_reply(text: str, message_id: str, chat_type: str, user_id: str) -> None:
        """通过飞书 REST API 发送回复。"""
        from lark_oapi.api.im.v1.model import (
            ReplyMessageRequest,
            ReplyMessageRequestBody,
        )
        from lark_oapi.api.im.v1.version import V1 as ImV1
        from lark_oapi.core.model.request_option import RequestOption

        try:
            req = (
                ReplyMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .msg_type("text")
                    .content(text)
                    .build()
                )
                .build()
            )
            resp = await http_client.im.v1.message.areply(req)
            if resp.Success():
                logger.info("✅ 回复成功: msg_id={}", message_id[:8] if message_id else "?")
            else:
                logger.error("❌ 回复失败: code={} msg={}", resp.Code, resp.Msg)
        except Exception as e:
            logger.error("发送回复异常: {}", e)
            raise

    handler = MessageHandler(
        llm_client=llm_client,
        send_reply_fn=send_reply,
        trigger_mode=bot_cfg.get("trigger_mode", "at_or_direct"),
        response_scope=bot_cfg.get("response_scope", "group_and_dm"),
        strip_at_placeholder=bot_cfg.get("strip_at_placeholder", True),
        max_message_length=bot_cfg.get("max_message_length", 5000),
        max_reply_length=bot_cfg.get("max_reply_length", 4000),
        max_messages_per_user=ctx_cfg.get("max_messages_per_user", 20),
        ttl_seconds=ctx_cfg.get("ttl_seconds", 0),
        mcp_manager=mcp_manager,
        use_mcp_tools=mcp_cfg.get("enabled", False),
    )

    # ── 4. BotEngine (WebSocket) ───────────────────────────────
    feishu_cfg = cfg["feishu"]

    engine = BotEngine(
        app_id=feishu_cfg["app_id"],
        app_secret=feishu_cfg["app_secret"],
        encrypt_key=feishu_cfg.get("encrypt_key", ""),
        token=feishu_cfg.get("token", ""),
        priority=feishu_cfg["websocket"].get("priority", 10),
        events=feishu_cfg["websocket"].get("events", ["im.message.receive_v1"]),
        rate_limit=rl_cfg,
        handler=handler,
        send_reply_fn=lambda t, mid, ct, uid: send_reply(t, mid, ct, uid),
    )

    # 挂在 app.state，供 shutdown 使用
    app.state.engine = engine
    app.state.handler = handler

    # ── 5. 启动引擎 ────────────────────────────────────────────
    await engine.start()
    logger.info("🤖 飞书机器人已就绪!")


@app.on_event("shutdown")
async def shutdown_event() -> None:
    engine: BotEngine = getattr(app.state, "engine", None)
    handler = getattr(app.state, "handler", None)
    if handler and handler._mcp_manager:
        await handler._mcp_manager.close()
        logger.info("已关闭所有 MCP 连接")
    if engine:
        await engine.stop()
        logger.info("已断开飞书 WebSocket 连接")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "feishu-bot"}


# ── 模块级导入（避免循环引用） ──────────────────────────────────────
from bot.engine import BotEngine
from bot.handler import MessageHandler


# ── 直接运行 ───────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))

    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        log_level="info",
        reload=False,
    )
