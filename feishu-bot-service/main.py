"""飞书机器人后端服务 — 入口点。

架构:
    1. bot/engine.py  → WS 线程接收 im.message.receive_v1，投回主 loop
    2. bot/handler.py → 解析、过滤 @占位符、路由群聊/私聊
    3. llm/client.py  → 调用 OpenAI 兼容大模型 API
    4. 飞书 REST API  → areply() 回复消息

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

import json
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

# ── 环境变量 ──────────────────────────────────────────────────────
from dotenv import load_dotenv

load_dotenv()

# ── 日志 ─────────────────────────────────────────────────────────
from loguru import logger

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# ── 配置加载 ──────────────────────────────────────────────────────
from utils.config import get_config

from bot.engine import BotEngine
from bot.handler import MessageHandler


def setup_logging() -> None:
    cfg = get_config()
    level = cfg.get("logging", {}).get("level", LOG_LEVEL)
    fmt = cfg.get("logging", {}).get(
        "format", "{time:YYYY-MM-DD HH:mm:ss} | {level} | {name} | {message}"
    )
    logger.remove()
    logger.add(sys.stderr, level=level.upper(), format=fmt)


def _build_send_reply(http_client: Any):
    """返回一个把文本回复发回飞书的协程函数。"""

    async def send_reply(
        text: str, message_id: str, chat_type: str, user_id: str
    ) -> None:
        from lark_oapi.api.im.v1.model import (
            ReplyMessageRequest,
            ReplyMessageRequestBody,
        )

        if not message_id:
            logger.error("缺少 message_id，无法回复")
            return

        try:
            req = (
                ReplyMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .msg_type("text")
                    # 飞书要求 content 是 JSON 串，不能是裸文本
                    .content(json.dumps({"text": text}, ensure_ascii=False))
                    .build()
                )
                .build()
            )
            resp = await http_client.im.v1.message.areply(req)
            # 注意是小写 success()；SDK 没有 Success()，写错会每次回复都 AttributeError
            if resp.success():
                logger.info("✅ 回复成功: msg_id={}", message_id[:8])
            else:
                logger.error("❌ 回复失败: code={} msg={}", resp.code, resp.msg)
        except Exception as e:
            logger.error("发送回复异常: {}", e)
            raise

    return send_reply


@asynccontextmanager
async def lifespan(app: "FastAPI") -> AsyncIterator[None]:
    """启动时初始化所有组件，关闭时释放。"""
    setup_logging()
    logger.info("=== 飞书机器人后端服务启动 ===")

    cfg = get_config()
    feishu_cfg = cfg["feishu"]
    app_id = feishu_cfg.get("app_id") or ""
    app_secret = feishu_cfg.get("app_secret") or ""
    if not app_id or not app_secret:
        raise RuntimeError(
            "缺少飞书应用凭证：请设置 FEISHU_APP_ID / FEISHU_APP_SECRET"
        )

    # ── 1. HTTP Client (用于发消息) ─────────────────────────────
    # 必须带上凭证，否则拿不到 tenant_access_token，所有回复都会失败
    from lark_oapi.client import ClientBuilder

    http_client = ClientBuilder().app_id(app_id).app_secret(app_secret).build()

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

    # ── 3. MCP Manager（可选） ─────────────────────────────────
    mcp_manager = None
    mcp_cfg = cfg.get("mcp", {})
    if mcp_cfg.get("enabled") and mcp_cfg.get("servers"):
        from mcp_plugin.manager import MCPManager

        mcp_manager = MCPManager(mcp_cfg["servers"])
        n_tools = await mcp_manager.connect()
        logger.info("🔌 MCP initialized: {} tools loaded", n_tools)

    # ── 4. Handler ─────────────────────────────────────────────
    bot_cfg = cfg.get("bot", {})
    ctx_cfg = cfg.get("context", {})
    rl_cfg = cfg.get("rate_limit", {})

    handler = MessageHandler(
        llm_client=llm_client,
        send_reply_fn=_build_send_reply(http_client),
        trigger_mode=bot_cfg.get("trigger_mode", "at_or_direct"),
        response_scope=bot_cfg.get("response_scope", "group_and_dm"),
        strip_at_placeholder=bot_cfg.get("strip_at_placeholder", True),
        max_message_length=bot_cfg.get("max_message_length", 5000),
        max_reply_length=bot_cfg.get("max_reply_length", 4000),
        max_messages_per_user=ctx_cfg.get("max_messages_per_user", 20),
        ttl_seconds=ctx_cfg.get("ttl_seconds", 0),
        rate_limit=rl_cfg,
        mcp_manager=mcp_manager,
        use_mcp_tools=mcp_cfg.get("enabled", False),
    )

    # ── 5. BotEngine (WebSocket) ───────────────────────────────
    engine = BotEngine(
        app_id=app_id,
        app_secret=app_secret,
        encrypt_key=feishu_cfg.get("encrypt_key", ""),
        token=feishu_cfg.get("token", ""),
        priority=feishu_cfg.get("websocket", {}).get("priority", 10),
        events=feishu_cfg.get("websocket", {}).get(
            "events", ["im.message.receive_v1"]
        ),
        rate_limit=rl_cfg,
        handler=handler,
    )

    app.state.engine = engine
    app.state.handler = handler

    await engine.start()
    logger.info("🤖 飞书机器人已就绪!")

    try:
        yield
    finally:
        if handler._mcp_manager:
            await handler._mcp_manager.close()
            logger.info("已关闭所有 MCP 连接")
        await engine.stop()


# ── FastAPI 应用（健康检查等） ───────────────────────────────────────
from fastapi import FastAPI

app = FastAPI(
    title="Feishu Bot Service",
    description="飞书机器人后端 — WebSocket 监听 + LLM 对话回传",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "feishu-bot"}


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
