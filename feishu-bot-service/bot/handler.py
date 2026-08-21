"""消息处理器 — 过滤 @占位符、路由群聊/私聊，驱动 LLM 并回复飞书。

字段命名以 lark-oapi SDK 的实际模型为准（曾因写错字段名导致所有消息被静默丢弃）：

    P2ImMessageReceiveV1          外层事件对象
      ├─ header                   EventHeader
      └─ event                    P2ImMessageReceiveV1Data  ← 注意是 event，不是 data
           ├─ sender              EventSender
           │    ├─ sender_id      UserId（对象，有 open_id/user_id/union_id，不是 dict）
           │    └─ sender_type    str  ← 机器人判定看这里，不是 sender_id.member_type
           └─ message             EventMessage
                ├─ message_type   str  ← 不是 msg_type
                ├─ content        str  ← JSON 串 '{"text":"..."}'，直接挂在 message 上，无 body
                ├─ chat_id / chat_type / message_id
                └─ mentions       List[MentionEvent]（key 为正文里的 @ 占位符）
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Awaitable, Callable

from loguru import logger

from bot.context import ConversationContext


class MessageHandler:
    """核心消息处理管道。

    流程:
        1. 忽略机器人自己/其他 bot 的消息，只处理文本
        2. 检查响应范围（response_scope）与触发模式（trigger_mode）
        3. 过滤消息长度 + 速率限制
        4. 去除 @占位符
        5. MCP Tool 路由与执行（可选）
        6. 追加对话上下文并调用 LLM
        7. 将回复发送到飞书
    """

    def __init__(
        self,
        llm_client: Any,
        *,
        send_reply_fn: Callable[[str, str, str, str], Awaitable[None]],
        trigger_mode: str = "at_or_direct",   # at_or_direct | only_at
        response_scope: str = "group_and_dm",  # group_and_dm | group_only | dm_only
        strip_at_placeholder: bool = True,
        max_message_length: int = 5000,
        max_reply_length: int = 4000,
        max_messages_per_user: int = 20,
        ttl_seconds: int = 0,
        rate_limit: dict[str, Any] | None = None,
        mcp_manager: Any | None = None,      # MCPManager or None
        use_mcp_tools: bool = True,          # 是否启用 MCP 工具调用
    ) -> None:
        self._llm = llm_client
        self.send_reply_fn = send_reply_fn
        self.trigger_mode = trigger_mode
        self.response_scope = response_scope
        self.strip_at_placeholder = strip_at_placeholder
        self.max_message_length = max_message_length
        self.max_reply_length = max_reply_length
        # 之前漏了这行：_check_rate_limit 读 self.rate_limit，每条消息必 AttributeError
        self.rate_limit = rate_limit or {}
        self._context = ConversationContext(
            max_messages_per_user=max_messages_per_user,
            ttl_seconds=ttl_seconds,
        )
        self._mcp_manager = mcp_manager
        self._use_mcp_tools = use_mcp_tools

        # 简易 TokenBucket
        self._user_buckets: dict[str, list[float]] = {}
        self._global_times: list[float] = []
        self._lock = asyncio.Lock()

    # --------------------------------------------------------------- public

    async def handle(self, event: Any) -> None:
        """处理一条飞书消息事件。

        Args:
            event: SDK 回调传入的 ``P2ImMessageReceiveV1``（外层对象）。
                这里刻意用 getattr 而非 isinstance 取字段 —— 之前的实现用
                ``isinstance(..., P2ImMessageReceiveV1Data)`` 卡住，而实际传入的是
                外层类型，导致直接 return，消息全丢。
        """
        data = getattr(event, "event", None)
        if data is None:
            logger.warning("事件缺少 event 字段，跳过: {}", type(event).__name__)
            return

        msg = getattr(data, "message", None)
        sender = getattr(data, "sender", None)
        if msg is None:
            logger.warning("事件缺少 message，跳过")
            return

        message_type = getattr(msg, "message_type", "") or ""
        chat_type = getattr(msg, "chat_type", "") or ""
        message_id = getattr(msg, "message_id", "") or ""
        mentions = getattr(msg, "mentions", None) or []

        sender_type = getattr(sender, "sender_type", "") or ""
        sender_id = getattr(sender, "sender_id", None)
        user_id = getattr(sender_id, "open_id", None) or "unknown"

        room_id = (getattr(msg, "chat_id", "") or "") if chat_type == "group" else ""
        text_raw = self._extract_text(msg)

        # 1. 忽略其他机器人的消息（避免 bot 互相刷）
        if sender_type == "app":
            logger.debug("来自应用的消息，跳过")
            return

        # 2. 只处理文本消息
        if message_type != "text":
            logger.debug("非文本消息 {}, 跳过", message_type or "(空)")
            return

        # 3. 检查响应范围
        if not self._check_scope(chat_type):
            logger.debug(
                "不在响应范围内: scope={} chat_type={}", self.response_scope, chat_type
            )
            return

        # 4. 检查触发模式
        if not self._check_trigger(chat_type, mentions):
            logger.debug("未满足触发条件 (trigger_mode={})", self.trigger_mode)
            return

        # 5. 过滤长度
        if len(text_raw) > self.max_message_length:
            logger.warning("消息超长 ({}), 跳过", len(text_raw))
            return

        # 6. 速率限制
        if not await self._check_rate_limit(user_id):
            logger.warning("用户 {}... 超出速率限制，跳过", user_id[:8])
            return

        # 7. 清洗文本（去 @占位符）
        clean_text = self._clean_message(text_raw, mentions)
        if not clean_text:
            logger.info("清理后消息为空，跳过")
            return

        session_key = ConversationContext.build_key(room_id, user_id)
        logger.info(
            "处理消息: user={} chat={} msg={} len={}",
            user_id[:8],
            room_id[:8] if room_id else "p2p",
            message_id[:8] if message_id else "?",
            len(clean_text),
        )

        # 8. 构建历史消息列表 + 追加 user 消息
        history = self._context.get_messages(session_key)
        await self._context.add_message(session_key, "user", clean_text)
        llm_messages = history + [{"role": "user", "content": clean_text}]

        # 9. 调用 LLM。"你有哪些工具"这类元问题单独处理：LLM 从不知道真实
        # 工具清单（function calling 也只给它候选子集），直接问只会凭训练
        # 知识编造工具名，所以这里把真实清单喂给它，不走工具调用。
        try:
            if self._is_tool_inventory_query(clean_text):
                reply = await self._answer_tool_inventory(llm_messages)
            else:
                reply = await self._chat_with_mcp_tools(llm_messages, clean_text)
        except Exception as e:
            logger.error("LLM 调用失败: {}", e)
            reply = "抱歉，服务暂时不可用，请稍后再试。"

        # 11. 截断回复
        reply = (reply or "")[: self.max_reply_length]
        if not reply:
            reply = "（无回复内容）"

        # 12. assistant 消息加入历史
        await self._context.add_message(session_key, "assistant", reply)

        # 13. 发送回复
        try:
            await self.send_reply_fn(reply, message_id, chat_type, user_id)
        except Exception as e:
            logger.error("发送回复失败: {}", e)

    # ------------------------------------------------------------------ MCP tool calling

    #: 每轮塞给模型的候选工具数量上限。116 个工具的完整 schema 一次性全传约
    #: 3 万 token，每条消息都要重复付这个成本——先用 ToolRouter 的关键词匹配
    #: 粗筛一批候选（宁可筛多一点，让模型自己判断该不该调），而不是精确单选。
    CANDIDATE_TOOL_COUNT = 20

    #: 工具调用循环的最大轮数，防止模型一直调工具不收敛导致这条消息卡死。
    MAX_TOOL_ROUNDS = 4

    #: 这些前缀的工具（长期记忆）每轮都强制放进候选，不受关键词命中率影响——
    #: 一句寒暄跟"记忆"毫无关键词重合，但模型仍应该能随时读写长期记忆。
    ALWAYS_AVAILABLE_TOOL_PREFIXES = ("memory-",)

    #: "你有哪些工具" 这类元问题的启发式识别：既要出现"工具/tool"类词，
    #: 也要出现"清单/list"类词，避免"帮我用搜索工具查一下xxx"被误判。
    _TOOL_WORDS = ("工具", "tool", "mcp")
    _INVENTORY_WORDS = ("有哪些", "哪些", "list", "清单", "列表", "都有什么", "能做什么", "支持哪些")

    @classmethod
    def _is_tool_inventory_query(cls, text: str) -> bool:
        lower = text.lower()
        has_tool_word = any(w in lower for w in cls._TOOL_WORDS)
        has_inventory_word = any(w.lower() in lower for w in cls._INVENTORY_WORDS)
        return has_tool_word and has_inventory_word

    async def _answer_tool_inventory(self, llm_messages: list[dict]) -> str:
        """如实回答"有哪些工具"：把真实工具名+简述喂给 LLM，不走工具调用。

        不这样做的话，LLM 既没被给候选 schema（用户没问具体任务），也没有
        任何真实工具清单可看，只会凭训练知识里对 MCP 生态的印象编答案——
        这正是之前"编出 AnySearch/Sorftime"那次事故的根因。
        """
        if self._mcp_manager and self._mcp_manager.is_connected:
            tools = self._mcp_manager.list_all_tools()
            lines = [f"- {t.name}: {(t.description or '').strip()[:60]}" for t in tools]
            catalog = "\n".join(lines) if lines else "（当前没有可用工具）"
        else:
            catalog = "（当前没有连接任何 MCP 工具）"

        augmented = list(llm_messages)
        augmented[-1] = dict(augmented[-1])
        augmented[-1]["content"] = (
            augmented[-1]["content"]
            + "\n\n--- 当前环境实际可调用的 MCP 工具清单（如实回答，不要提到这份清单之外的工具名）---\n"
            + catalog
        )
        return await self._llm.chat(augmented)

    async def _chat_with_mcp_tools(self, llm_messages: list[dict], clean_text: str) -> str:
        """真正的任务型请求：走 function calling，让模型自己决定调不调工具、调哪个。"""
        tools_schema = self._select_candidate_tools(clean_text)
        messages = list(llm_messages)

        for _ in range(self.MAX_TOOL_ROUNDS):
            result = await self._llm.chat_with_tools(messages, tools_schema)
            if not result.tool_calls:
                return result.content or ""

            messages.append(
                {
                    "role": "assistant",
                    "content": result.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": tc.arguments},
                        }
                        for tc in result.tool_calls
                    ],
                }
            )
            for tc in result.tool_calls:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": await self._execute_tool_call(tc),
                    }
                )

        logger.warning("MCP 工具调用超过 {} 轮仍未收敛，强制要一个文字回复", self.MAX_TOOL_ROUNDS)
        final = await self._llm.chat(messages)
        return final or "抱歉，这个请求需要的步骤有点多，我没能在限定轮数内完成，麻烦拆分一下问题重新问我。"

    async def _execute_tool_call(self, tool_call: Any) -> str:
        """执行一次模型发起的工具调用，返回喂回给模型的文本（成功或错误都要喂回，不能抛出）。"""
        try:
            arguments = json.loads(tool_call.arguments) if tool_call.arguments else {}
        except (TypeError, ValueError) as e:
            logger.warning("工具 {} 的参数不是合法 JSON: {}", tool_call.name, e)
            return json.dumps({"error": f"invalid arguments JSON: {e}"}, ensure_ascii=False)

        try:
            result = await self._mcp_manager.call_tool_by_name(tool_call.name, arguments)
        except Exception as e:
            logger.warning("MCP 工具 {} 调用失败: {}", tool_call.name, e)
            return json.dumps({"error": str(e)}, ensure_ascii=False)

        if result["ok"]:
            logger.info("🔧 Used MCP tool '{}'", tool_call.name)
            return result["result"] or "(空结果)"
        logger.warning("⚠️ MCP tool '{}' error: {}", tool_call.name, result["errors"])
        return json.dumps({"error": "; ".join(result["errors"])}, ensure_ascii=False)

    def _select_candidate_tools(self, clean_text: str) -> list[dict] | None:
        """关键词粗筛候选 + 强制带上长期记忆工具，转成 OpenAI function schema。

        候选为空时返回 ``None``（不是 ``[]``）—— 某些 OpenAI 兼容网关把
        ``tools=[]`` 当成"禁止调用任何工具"，跟"不提供工具"是两个语义。

        ``ToolRouter`` 每次都用当前工具列表现建，不缓存实例——早期实现缓存
        过一版，会导致 MCP 工具列表刷新后路由索引跟着变旧（同一个 server
        两次 connect 实测返回过 64 个和 116 个不同数量的工具，说明这个列表
        并非一成不变）。实测对 64 个真实工具重建+路由一次约 1ms，相比 LLM
        一轮秒级往返可以忽略，用"现建"换"永远不会读到过期索引"是划算的。
        """
        if not (self._use_mcp_tools and self._mcp_manager and self._mcp_manager.is_connected):
            return None

        all_tools = self._mcp_manager.list_all_tools()
        if not all_tools:
            return None

        from mcp_plugin.tools import ToolRouter

        matched = ToolRouter(all_tools).route(clean_text, top_k=self.CANDIDATE_TOOL_COUNT)

        candidates = {tool.name: tool for _idx, tool in matched}

        # 命名空间扩展：关键词只要命中某个命名空间下的任意一个工具，就把该
        # 命名空间下全部工具都放进候选——真实事故：用户要"创建"飞书文档，
        # create_feishu_document 的描述里恰好没出现查询里的"title"/"url"这两
        # 个词，字面匹配打 0 分被直接漏掉；反而是语义不相关的
        # get_feishu_folder_files 因为字面撞上了"url"进了候选。中文提问和
        # 纯英文工具描述之间字面重合本来就稀薄，逐词打分在这种场景下不可靠。
        # 一旦确认某个服务"在场"（哪怕命中信号很弱），就把精确挑选的工作交
        # 给真正懂语言语义的模型，而不是继续用字面匹配去猜。
        matched_namespaces = {self._tool_namespace(tool.name) for _idx, tool in matched}
        if matched_namespaces:
            for tool in all_tools:
                if self._tool_namespace(tool.name) in matched_namespaces:
                    candidates[tool.name] = tool

        for tool in all_tools:
            if tool.name.startswith(self.ALWAYS_AVAILABLE_TOOL_PREFIXES):
                candidates[tool.name] = tool

        if not candidates:
            return None
        return [tool.to_openai_schema() for tool in candidates.values()]

    @staticmethod
    def _tool_namespace(tool_name: str) -> str:
        """从工具名推出所属命名空间。

        网关按 ``<服务名>-<原始工具名>`` 拼接命名（如
        ``feishu-mcp-create_feishu_document`` -> ``feishu-mcp``，
        ``memory-add_observations`` -> ``memory``），原始工具名内部一律用
        下划线，服务名和工具名之间的最后一个短横线就是天然的分隔点——取最后
        一个 ``-`` 之前的部分即可，不需要为每个服务维护中英文词表。
        """
        return tool_name.rsplit("-", 1)[0] if "-" in tool_name else tool_name

    @staticmethod
    def _extract_text(msg: Any) -> str:
        """从 EventMessage 提取纯文本。

        ``content`` 是 JSON 串（文本消息形如 ``{"text":"hello"}``），必须解析出
        ``text`` 字段 —— 直接把整个 JSON 丢给 LLM 是之前的另一个 bug。
        """
        raw = getattr(msg, "content", "") or ""
        if not raw:
            return ""
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("消息 content 不是合法 JSON，按原文处理")
            return str(raw)
        if isinstance(payload, dict):
            return str(payload.get("text", "") or "")
        return ""

    def _check_scope(self, chat_type: str) -> bool:
        if chat_type == "group":
            return self.response_scope in ("group_and_dm", "group_only")
        return self.response_scope in ("group_and_dm", "dm_only")

    def _check_trigger(self, chat_type: str, mentions: list[Any]) -> bool:
        """判断是否满足触发条件。"""
        if self.trigger_mode != "only_at":
            return True          # at_or_direct：群聊私聊都响应
        if chat_type != "group":
            return True          # 私聊无需 @
        # 群聊 only_at：正文里必须有 @。注意 mentions 里被 @ 的可能是别人 ——
        # 事件里拿不到 bot 自身 open_id，这里只能判断"有人被 @"。
        return bool(mentions)

    def _clean_message(self, text: str, mentions: list[Any] | None = None) -> str:
        """去除群聊中的 @占位符。

        飞书把被 @ 的人在正文里替换成占位符（如 ``@_user_1``），真实身份放在
        ``mentions[].key``。优先按 key 精确删除，再兜底清掉残留的 ``@_xxx``。
        不要用宽泛的 ``@\\w+`` —— 那会把正文里正常的"@某人"也删掉。
        """
        if not self.strip_at_placeholder:
            return text
        cleaned = text
        for m in mentions or []:
            key = getattr(m, "key", None)
            if key:
                cleaned = cleaned.replace(key, "")
        cleaned = re.sub(r"@_\w+", "", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        return cleaned

    async def _check_rate_limit(self, user_id: str) -> bool:
        async with self._lock:
            now = asyncio.get_running_loop().time()
            limit_global = self.rate_limit.get("global_per_second", 10)
            limit_user = self.rate_limit.get("per_user_per_second", 3)

            self._global_times = [t for t in self._global_times if now - t < 1.0]
            if len(self._global_times) >= limit_global:
                return False

            bucket = self._user_buckets.setdefault(user_id, [])
            bucket[:] = [t for t in bucket if now - t < 1.0]
            if len(bucket) >= limit_user:
                return False

            # 两个配额都够了才记账，避免被单用户限流的请求白占全局配额
            self._global_times.append(now)
            bucket.append(now)
            return True
