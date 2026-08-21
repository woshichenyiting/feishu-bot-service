"""bot/handler.py 的事件解析 + MCP function-calling 测试。

第一部分覆盖一批"字段名写错 → 消息被静默丢弃"的真实 bug（isinstance 误判、
msg_type/message_type、body.content/content、JSON 未解析、rate_limit 未赋值）。

第二部分覆盖把"关键词路由单选一个工具、结果拼字符串"换成"候选过滤 +
function calling"之后的行为：
    - "你有哪些工具"这类元问题必须如实回答真实清单，不能让 LLM 凭训练知识
      编造 AnySearch/Sorftime 这类不存在的工具名（真实事故，见对应 commit）。
    - 任务型请求要把候选工具 schema 交给模型，模型发起 tool_calls 后要真的
      执行 MCP 工具、把结果喂回去、再要一轮最终回复。
    - 参数不是合法 JSON、工具执行报错都不能让整条消息处理崩掉，要把错误喂
      回模型而不是抛异常。
    - 循环有轮数上限，不能因为模型一直调工具不收敛而卡死一条消息。
    - memory-* 工具（长期记忆）每轮都强制在候选里，不受关键词命中率影响。
"""

from __future__ import annotations

import pytest
from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1

from bot.handler import MessageHandler
from llm.client import ChatResult, ToolCallRequest


def _make_event(
    *,
    text: str = "hello",
    message_type: str = "text",
    chat_type: str = "group",
    sender_type: str = "user",
    mentions: list[dict] | None = None,
    open_id: str = "ou_123",
    message_id: str = "om_1",
) -> P2ImMessageReceiveV1:
    """按飞书真实事件 payload 构造 SDK 对象。"""
    import json

    return P2ImMessageReceiveV1(
        {
            "schema": "2.0",
            "header": {"event_id": "e1", "event_type": "im.message.receive_v1"},
            "event": {
                "sender": {
                    "sender_id": {"open_id": open_id},
                    "sender_type": sender_type,
                },
                "message": {
                    "message_id": message_id,
                    "chat_id": "oc_1",
                    "chat_type": chat_type,
                    "message_type": message_type,
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                    "mentions": mentions or [],
                },
            },
        }
    )


class _FakeLLM:
    """假 LLM：默认走一轮直接给 reply；传 tool_call_script 可以编排多轮
    tool_calls -> 最终回复 的脚本，模拟 function calling 的往返。"""

    def __init__(self, reply: str = "pong", tool_call_script: list[ChatResult] | None = None) -> None:
        self.reply = reply
        self.calls: list[list[dict]] = []               # chat() 调用记录
        self.tool_calls_history: list[list[dict]] = []  # chat_with_tools() 的 messages 参数
        self.tools_history: list[list[dict] | None] = []  # chat_with_tools() 的 tools 参数
        self._script = list(tool_call_script) if tool_call_script else None

    async def chat(self, messages: list[dict]) -> str:
        self.calls.append(messages)
        return self.reply

    async def chat_with_tools(self, messages: list[dict], tools=None) -> ChatResult:
        self.tool_calls_history.append(messages)
        self.tools_history.append(tools)
        if self._script:
            return self._script.pop(0)
        return ChatResult(content=self.reply, tool_calls=[])


class _FakeTool:
    def __init__(self, name: str, description: str, input_schema: dict | None = None) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema or {"type": "object", "properties": {}}

    def to_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class _FakeManager:
    """假 MCPManager：list_all_tools 固定返回一批工具，call_tool_by_name 可注入。"""

    def __init__(self, tools: list[_FakeTool], call_tool_by_name=None) -> None:
        self._tools = tools
        self.is_connected = True
        self.calls: list[tuple[str, dict]] = []
        self._call_tool_by_name = call_tool_by_name

    def list_all_tools(self) -> list[_FakeTool]:
        return self._tools

    async def call_tool_by_name(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, arguments))
        if self._call_tool_by_name:
            return await self._call_tool_by_name(name, arguments)
        return {"ok": True, "result": "ok", "errors": []}


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple] = []

    async def __call__(
        self, text: str, message_id: str, chat_type: str, user_id: str
    ) -> None:
        self.sent.append((text, message_id, chat_type, user_id))


def _make_handler(llm: _FakeLLM, recorder: _Recorder, **kwargs) -> MessageHandler:
    return MessageHandler(llm_client=llm, send_reply_fn=recorder, **kwargs)


# ------------------------------------------------------------- 事件解析 / 字段映射


@pytest.mark.asyncio
async def test_text_message_reaches_llm_and_reply():
    """完整链路：外层事件对象 → 解析出正文 → 调 LLM（function calling 通道）→ 回复飞书。

    这条用例同时锁住 isinstance 误判、msg_type/message_type、body.content/content、
    JSON 未解析、以及 self.rate_limit 未赋值这五个 bug —— 任何一个复发它都会红。
    """
    llm, recorder = _FakeLLM("pong"), _Recorder()
    handler = _make_handler(llm, recorder)

    await handler.handle(_make_event(text="hello"))

    assert len(llm.tool_calls_history) == 1, "LLM 未被调用——消息在 handler 里被丢了"
    assert llm.tool_calls_history[0][-1] == {"role": "user", "content": "hello"}
    assert recorder.sent == [("pong", "om_1", "group", "ou_123")]


@pytest.mark.asyncio
async def test_at_placeholder_stripped_before_llm():
    """群聊 @机器人 时，正文里的 @_user_1 占位符要去掉再送 LLM。"""
    llm, recorder = _FakeLLM(), _Recorder()
    handler = _make_handler(llm, recorder)

    await handler.handle(
        _make_event(text="@_user_1 今天天气如何", mentions=[{"key": "@_user_1"}])
    )

    assert llm.tool_calls_history[0][-1]["content"] == "今天天气如何"


@pytest.mark.asyncio
async def test_bot_messages_ignored():
    """其他机器人的消息不处理（靠 sender_type，不是 sender_id.member_type）。"""
    llm, recorder = _FakeLLM(), _Recorder()
    handler = _make_handler(llm, recorder)

    await handler.handle(_make_event(sender_type="app"))

    assert llm.calls == []
    assert llm.tool_calls_history == []
    assert recorder.sent == []


@pytest.mark.asyncio
async def test_non_text_message_ignored():
    llm, recorder = _FakeLLM(), _Recorder()
    handler = _make_handler(llm, recorder)

    await handler.handle(_make_event(message_type="image"))

    assert llm.tool_calls_history == []


@pytest.mark.asyncio
async def test_rate_limit_blocks_second_message():
    """速率限制生效（且不再因 self.rate_limit 缺失而 AttributeError）。"""
    llm, recorder = _FakeLLM(), _Recorder()
    handler = _make_handler(
        llm, recorder, rate_limit={"per_user_per_second": 1, "global_per_second": 10}
    )

    await handler.handle(_make_event(text="第一条"))
    await handler.handle(_make_event(text="第二条"))

    assert len(llm.tool_calls_history) == 1, "同一用户 1 秒内第二条应被限流"


@pytest.mark.asyncio
async def test_dm_only_scope_skips_group():
    llm, recorder = _FakeLLM(), _Recorder()
    handler = _make_handler(llm, recorder, response_scope="dm_only")

    await handler.handle(_make_event(chat_type="group"))
    assert llm.tool_calls_history == []

    await handler.handle(_make_event(chat_type="p2p"))
    assert len(llm.tool_calls_history) == 1


@pytest.mark.asyncio
async def test_only_at_requires_mention_in_group():
    llm, recorder = _FakeLLM(), _Recorder()
    handler = _make_handler(llm, recorder, trigger_mode="only_at")

    # 群聊无 @ → 不响应
    await handler.handle(_make_event(chat_type="group", mentions=[]))
    assert llm.tool_calls_history == []

    # 群聊有 @ → 响应
    await handler.handle(
        _make_event(chat_type="group", text="@_user_1 在吗", mentions=[{"key": "@_user_1"}])
    )
    assert len(llm.tool_calls_history) == 1

    # 私聊无需 @
    await handler.handle(_make_event(chat_type="p2p", text="在吗"))
    assert len(llm.tool_calls_history) == 2


@pytest.mark.asyncio
async def test_llm_failure_still_replies():
    """LLM 抛错时要给用户兜底回复，不能静默失败。"""

    class _BoomLLM:
        async def chat_with_tools(self, messages, tools=None):
            raise RuntimeError("upstream 503")

        async def chat(self, messages):
            raise RuntimeError("upstream 503")

    recorder = _Recorder()
    handler = MessageHandler(llm_client=_BoomLLM(), send_reply_fn=recorder)

    await handler.handle(_make_event())

    assert len(recorder.sent) == 1
    assert "暂时不可用" in recorder.sent[0][0]


def test_extract_text_parses_json_envelope():
    """content 是 JSON 串，要取出 text，而不是把整个 JSON 丢给 LLM。"""

    class _Msg:
        content = '{"text":"你好"}'

    assert MessageHandler._extract_text(_Msg()) == "你好"


def test_extract_text_handles_empty_and_malformed():
    class _Empty:
        content = ""

    class _Bad:
        content = "not json"

    assert MessageHandler._extract_text(_Empty()) == ""
    # 非法 JSON 退化为原文，至少不丢消息
    assert MessageHandler._extract_text(_Bad()) == "not json"


def test_clean_message_keeps_plain_at_text():
    """只删飞书的 @_xxx 占位符，不能误删正文里正常的 "@某人"。

    旧正则是 @[_a-zA-Z0-9一-鿿]+，会把"帮我 @张三 一下"里的 @张三 删掉。
    """
    handler = MessageHandler(llm_client=None, send_reply_fn=None)

    assert handler._clean_message("hello @_user_1 world") == "hello world"
    assert handler._clean_message("帮我 @张三 转达一下") == "帮我 @张三 转达一下"
    assert handler._clean_message("没有提及") == "没有提及"


def test_clean_message_uses_mention_keys():
    handler = MessageHandler(llm_client=None, send_reply_fn=None)

    class _M:
        key = "@_user_7"

    assert handler._clean_message("@_user_7 在吗", [_M()]) == "在吗"


# ------------------------------------------------------------- MCP function calling


@pytest.mark.asyncio
async def test_meta_tool_inventory_query_answers_from_real_catalog():
    """"你有哪些 mcp 的 tool 可以调用，给我个 list" 这类元问题，必须把真实工具
    清单喂给 LLM，不能让它凭训练知识编出 AnySearch/Sorftime 这种不存在的工具名
    （真实事故）。"""
    llm, recorder = _FakeLLM("这是根据真实清单给出的回答"), _Recorder()
    manager = _FakeManager(
        [
            _FakeTool("baidu-mcp-AIsearch", "百度 AI 搜索"),
            _FakeTool("memory-search_nodes", "查询知识图谱节点"),
        ]
    )
    handler = _make_handler(llm, recorder, mcp_manager=manager, use_mcp_tools=True)

    await handler.handle(
        _make_event(text="目前有哪些mcp的tool可以调用，给我个list", chat_type="p2p")
    )

    assert len(llm.calls) == 1, "元问题应该走普通 chat()，不应该走 function calling"
    assert llm.tool_calls_history == [], "元问题不应该触发任何工具调用"
    prompt = llm.calls[0][-1]["content"]
    assert "baidu-mcp-AIsearch" in prompt
    assert "memory-search_nodes" in prompt
    assert recorder.sent[0][0] == "这是根据真实清单给出的回答"


@pytest.mark.asyncio
async def test_tool_inventory_query_without_mcp_says_no_tools():
    llm, recorder = _FakeLLM("目前没有可用工具"), _Recorder()
    handler = _make_handler(llm, recorder)  # 不配 mcp_manager

    await handler.handle(_make_event(text="你有哪些工具？", chat_type="p2p"))

    prompt = llm.calls[0][-1]["content"]
    assert "没有连接任何 MCP 工具" in prompt


@pytest.mark.asyncio
async def test_tool_call_round_trip_executes_and_feeds_result_back():
    """任务型请求：模型发起 tool_calls → 真的执行 MCP 工具 → 结果喂回去 → 再要一轮最终回复。"""

    async def _call(name, arguments):
        assert name == "get_weather"
        assert arguments == {"city": "北京"}
        return {"ok": True, "result": "晴，25度", "errors": []}

    manager = _FakeManager(
        [_FakeTool("get_weather", "查询天气", {"type": "object", "properties": {"city": {"type": "string"}}})],
        call_tool_by_name=_call,
    )
    script = [
        ChatResult(content=None, tool_calls=[ToolCallRequest(id="call_1", name="get_weather", arguments='{"city": "北京"}')]),
        ChatResult(content="北京今天晴，25度", tool_calls=[]),
    ]
    llm = _FakeLLM(tool_call_script=script)
    recorder = _Recorder()
    handler = _make_handler(llm, recorder, mcp_manager=manager, use_mcp_tools=True)

    await handler.handle(_make_event(text="北京天气怎么样", chat_type="p2p"))

    assert recorder.sent[0][0] == "北京今天晴，25度"
    assert len(llm.tool_calls_history) == 2, "应该有两轮：第一轮拿到 tool_call，第二轮拿最终回复"
    assert manager.calls == [("get_weather", {"city": "北京"})]

    second_round = llm.tool_calls_history[1]
    tool_messages = [m for m in second_round if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call_1"
    assert tool_messages[0]["content"] == "晴，25度"

    assistant_messages = [m for m in second_round if m.get("role") == "assistant" and m.get("tool_calls")]
    assert len(assistant_messages) == 1, "第一轮的 assistant tool_calls 消息要原样追加回去，模型才知道自己刚发起过什么调用"


@pytest.mark.asyncio
async def test_tool_call_with_invalid_json_arguments_does_not_crash():
    """模型给的 arguments 不是合法 JSON 时，要把错误喂回模型，不能让整条消息处理崩掉。"""

    async def _call(name, arguments):
        raise AssertionError("参数解析失败时不应该真的执行工具")

    manager = _FakeManager([_FakeTool("get_weather", "查询天气")], call_tool_by_name=_call)
    script = [
        ChatResult(content=None, tool_calls=[ToolCallRequest(id="call_1", name="get_weather", arguments="{not valid json")]),
        ChatResult(content="抱歉，参数解析出了点问题", tool_calls=[]),
    ]
    llm = _FakeLLM(tool_call_script=script)
    recorder = _Recorder()
    handler = _make_handler(llm, recorder, mcp_manager=manager, use_mcp_tools=True)

    await handler.handle(_make_event(text="北京天气", chat_type="p2p"))

    assert recorder.sent[0][0] == "抱歉，参数解析出了点问题"
    tool_messages = [m for m in llm.tool_calls_history[1] if m.get("role") == "tool"]
    assert "error" in tool_messages[0]["content"]


@pytest.mark.asyncio
async def test_tool_execution_error_is_fed_back_not_raised():
    """MCP 工具执行本身报错（比如上游 503）时，同样要喂回模型而不是抛异常。"""

    async def _call(name, arguments):
        raise RuntimeError("upstream 503")

    manager = _FakeManager([_FakeTool("flaky-tool", "有时会挂的工具")], call_tool_by_name=_call)
    script = [
        ChatResult(content=None, tool_calls=[ToolCallRequest(id="call_1", name="flaky-tool", arguments="{}")]),
        ChatResult(content="这个工具暂时不可用", tool_calls=[]),
    ]
    llm = _FakeLLM(tool_call_script=script)
    recorder = _Recorder()
    handler = _make_handler(llm, recorder, mcp_manager=manager, use_mcp_tools=True)

    await handler.handle(_make_event(text="用一下那个工具", chat_type="p2p"))

    assert recorder.sent[0][0] == "这个工具暂时不可用"
    tool_messages = [m for m in llm.tool_calls_history[1] if m.get("role") == "tool"]
    assert "upstream 503" in tool_messages[0]["content"]


@pytest.mark.asyncio
async def test_tool_loop_caps_rounds_and_forces_final_answer():
    """模型一直发起 tool_calls 不收敛时，超过轮数上限要强制退回普通 chat() 拿一个文字回复，不能卡死。"""

    async def _call(name, arguments):
        return {"ok": True, "result": "还要继续", "errors": []}

    manager = _FakeManager([_FakeTool("loopy", "一个总想再调一次的工具")], call_tool_by_name=_call)
    llm = _FakeLLM(reply="强制给的最终回复")

    async def _always_tool_call(messages, tools=None):
        llm.tool_calls_history.append(messages)
        return ChatResult(content=None, tool_calls=[ToolCallRequest(id="x", name="loopy", arguments="{}")])

    llm.chat_with_tools = _always_tool_call
    recorder = _Recorder()
    handler = _make_handler(llm, recorder, mcp_manager=manager, use_mcp_tools=True)

    await handler.handle(_make_event(text="帮我循环一下", chat_type="p2p"))

    assert len(llm.tool_calls_history) == MessageHandler.MAX_TOOL_ROUNDS
    assert len(llm.calls) == 1, "超过轮数上限后应该退回普通 chat() 强制拿文字回复"
    assert recorder.sent[0][0] == "强制给的最终回复"


@pytest.mark.asyncio
async def test_memory_tools_always_included_regardless_of_keyword_match():
    """memory-* 工具（长期记忆）每轮都要在候选里，不受关键词命中率影响——
    一句寒暄跟"记忆"毫无关键词重合，模型仍应该能随时读写长期记忆。"""
    weather = _FakeTool("weather-lookup", "查询天气预报")
    memory = _FakeTool("memory-search_nodes", "在知识图谱里搜索节点")
    manager = _FakeManager([weather, memory])

    llm, recorder = _FakeLLM("好的"), _Recorder()
    handler = _make_handler(llm, recorder, mcp_manager=manager, use_mcp_tools=True)

    # 纯寒暄，跟 weather/memory 的关键词毫无重合
    await handler.handle(_make_event(text="今天心情不错", chat_type="p2p"))

    tools_passed = llm.tools_history[0]
    names = {t["function"]["name"] for t in (tools_passed or [])}
    assert "memory-search_nodes" in names


@pytest.mark.asyncio
async def test_candidate_filter_excludes_completely_unrelated_tools():
    """候选过滤要真的筛掉跟查询毫无关键词重合的工具，不能变成"全量传"。"""
    weather = _FakeTool("weather-lookup", "查询城市天气预报")
    unrelated = _FakeTool("email-send", "Send an email to a contact")
    manager = _FakeManager([weather, unrelated])

    llm, recorder = _FakeLLM("晴天"), _Recorder()
    handler = _make_handler(llm, recorder, mcp_manager=manager, use_mcp_tools=True)

    await handler.handle(_make_event(text="查一下北京天气", chat_type="p2p"))

    tools_passed = llm.tools_history[0]
    names = {t["function"]["name"] for t in (tools_passed or [])}
    assert "weather-lookup" in names
    assert "email-send" not in names


@pytest.mark.asyncio
async def test_candidate_tools_reflect_live_tool_list_not_stale_cache():
    """ToolRouter 不应该被缓存——manager 的工具列表在两条消息之间变了
    （真实观察到同一个 server 两次 connect 返回过 64/116 个不同数量的
    工具），第二条消息的候选必须看到新列表，不能还是第一条消息时的旧索引。"""
    weather = _FakeTool("weather-lookup", "查询天气预报")

    class _MutableManager:
        is_connected = True

        def __init__(self):
            self.tools = [weather]

        def list_all_tools(self):
            return self.tools

    manager = _MutableManager()
    llm, recorder = _FakeLLM("好的"), _Recorder()
    handler = _make_handler(llm, recorder, mcp_manager=manager, use_mcp_tools=True)

    await handler.handle(_make_event(text="查一下天气", chat_type="p2p"))
    first_names = {t["function"]["name"] for t in (llm.tools_history[0] or [])}
    assert first_names == {"weather-lookup"}

    # 模拟 MCP 重新连接后工具列表变化：换成一个新工具
    stock = _FakeTool("stock-price", "查询股票价格")
    manager.tools = [stock]

    await handler.handle(_make_event(text="查一下股票", chat_type="p2p"))
    second_names = {t["function"]["name"] for t in (llm.tools_history[1] or [])}
    assert second_names == {"stock-price"}, "候选没有反映最新的工具列表，说明路由索引被缓存住了"


@pytest.mark.asyncio
async def test_namespace_expansion_pulls_in_sibling_tools_with_zero_keyword_overlap():
    """真实事故复现：用户中文提问"帮我创建一个飞书文档"，
    create_feishu_document 的英文描述里恰好一个查询词都没有，字面匹配打 0
    分被漏掉；同命名空间下字面撞词的 search_feishu_documents 反而进了候选。
    命名空间扩展要把整个 feishu-mcp-* 命名空间都拉进来，包括零重合的
    create 工具，让模型有机会选对。"""
    searchable = _FakeTool(
        "feishu-mcp-search_feishu_documents",
        "Searches for documents in Feishu. Supports keyword and title queries via URL params.",
    )
    creatable = _FakeTool(
        "feishu-mcp-create_feishu_document",
        "Creates a brand new document and returns its identifier.",
    )
    other_namespace = _FakeTool("todoist-todoist_get_tasks", "List all pending todo tasks")
    manager = _FakeManager([searchable, creatable, other_namespace])

    llm, recorder = _FakeLLM("好的"), _Recorder()
    handler = _make_handler(llm, recorder, mcp_manager=manager, use_mcp_tools=True)

    # 中文查询跟 creatable 的英文描述毫无字面重合，只跟 searchable 里的
    # "title"/"url" 撞词
    await handler.handle(
        _make_event(text="帮我在飞书创建一个文档，title 用 URL 发给我", chat_type="p2p")
    )

    names = {t["function"]["name"] for t in (llm.tools_history[0] or [])}
    assert "feishu-mcp-search_feishu_documents" in names
    assert "feishu-mcp-create_feishu_document" in names, (
        "同命名空间的工具没有被拉进候选——命名空间扩展失效了"
    )
    assert "todoist-todoist_get_tasks" not in names, "不同命名空间不应该被无关扩展进来"


def test_tool_namespace_extraction():
    """命名空间提取：取最后一个 `-` 之前的部分（原始工具名内部一律用下划线）。"""
    assert MessageHandler._tool_namespace("feishu-mcp-create_feishu_document") == "feishu-mcp"
    assert MessageHandler._tool_namespace("baidu-mcp-AIsearch") == "baidu-mcp"
    assert MessageHandler._tool_namespace("ntfy-b-send_ntfy_notification") == "ntfy-b"
    assert MessageHandler._tool_namespace("memory-add_observations") == "memory"
    assert MessageHandler._tool_namespace("todoist-todoist_create_task") == "todoist"
    assert MessageHandler._tool_namespace("no_dash_at_all") == "no_dash_at_all"


@pytest.mark.asyncio
async def test_no_mcp_manager_still_calls_llm_without_tools():
    """没配 MCP（mcp.enabled=false）时，普通聊天要照常工作，只是不带 tools。"""
    llm, recorder = _FakeLLM("你好呀"), _Recorder()
    handler = _make_handler(llm, recorder)  # 不传 mcp_manager

    await handler.handle(_make_event(text="你好", chat_type="p2p"))

    assert llm.tools_history == [None]
    assert recorder.sent[0][0] == "你好呀"
