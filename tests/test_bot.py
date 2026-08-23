"""基础测试 — 验证项目可导入、配置加载正常。"""


def test_imports():
    """所有模块应可成功导入。"""
    from bot.engine import BotEngine
    from bot.handler import MessageHandler
    from bot.context import ConversationContext
    from llm.client import LLMClient, OpenAIClient
    from utils.config import get_config
    assert BotEngine is not None
    assert MessageHandler is not None
    assert ConversationContext is not None
    assert LLMClient is not None
    assert OpenAIClient is not None
    assert get_config is not None


def test_context_basic():
    """测试对话上下文管理。"""
    import asyncio
    from bot.context import ConversationContext
    ctx = ConversationContext(max_messages_per_user=2)

    key = ConversationContext.build_key("og_group_xxx", "o_user_1")
    # Initial state
    assert ctx.get_messages(key) == []

    async def _test():
        await ctx.add_message(key, "user", "你好")
        assert len(ctx.get_messages(key)) == 1
        await ctx.add_message(key, "assistant", "你好！有什么可以帮你？")
        assert len(ctx.get_messages(key)) == 2

        # Add more, should trigger window trimming
        await ctx.add_message(key, "user", "第二条问题")
        await ctx.add_message(key, "assistant", "这是第二个回答")
        await ctx.add_message(key, "user", "第三条问题")
        # After trim: should have max * 2 = 4 messages (but trimmed down)
        msg_list = ctx.get_messages(key)
        assert len(msg_list) <= 4  # sliding window limit

    asyncio.run(_test())


def test_handler_clean_message():
    """测试消息清洗（去 @占位符）。"""
    from bot.handler import MessageHandler
    from unittest.mock import MagicMock

    handler = MessageHandler(
        llm_client=MagicMock(),
        send_reply_fn=lambda *a: None,
        strip_at_placeholder=True,
    )

    assert handler._clean_message("hello @_user_1 world") == "hello world"
    assert handler._clean_message("你是谁 @小助") == "你是谁"  # Chinese chars supported
    assert handler._clean_message("没有提及") == "没有提及"


def test_llm_client_init():
    """测试 LLM Client 初始化（不需要实际 API Key）。"""
    from llm.client import OpenAIClient
    client = OpenAIClient(
        base_url="https://test.example.com/v1",
        api_key="fake-key-for-test",
        model="gpt-4o-mini",
    )
    assert client._model == "gpt-4o-mini"
    assert client._temperature == 0.7
