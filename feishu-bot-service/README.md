# 飞书机器人后端服务

基于 [lark-oapi](https://github.com/larksuite/oapi-sdk-python) SDK v1.x，实现"接收飞书消息 → 调用 LLM → 回复飞书"的完整闭环。

## 工作流程

```
用户发送消息到飞书群/私聊
        ↓
飞书推送中心 (WebSocket 长连接)
        ↓
lark-oapi WS Client ←→ BotEngine
        ↓
MessageHandler:
  ├─ 过滤其他机器人的消息
  ├─ 群聊中去 @占位符
  ├─ 触发模式判断 (at_or_direct / only_at)
  ├─ 速率限制
  └─ 上下文管理 (滑动窗口)
        ↓
LLM Client (OpenAI 兼容 API)
  ├─ gpt-4o
  ├─ qwen-plus (通义千问)
  ├─ glm-4 (智谱)
  └─ deepseek-v3
        ↓
飞书 REST API (areply)
        ↓
用户收到 AI 回复
```

## 快速开始

### 1. 安装依赖

```bash
cd feishu-bot-service
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置

```bash
# 复制环境变量文件
cp .env.example .env
# 编辑 .env 填入你的凭证
nano .env
```

`.env` 内容：
```env
FEISHU_APP_ID=cli_xxxxxxxxxx    # 飞书应用的 App ID
FEISHU_APP_SECRET=xxxxxxxxxxxx  # 飞书应用的 App Secret
LLM_API_KEY=sk-xxxxxxxxxxxx     # LLM API Key（通义/智谱/OpenAI）
```

可选：创建 `config.local.yaml` 覆盖 `config.yaml` 中的默认设置。

### 3. 运行

```bash
source .venv/bin/activate
python main.py
```

## Docker 部署

```bash
# 1. 复制项目到服务器
scp -r feishu-bot-service user@server:/opt/

# 2. 填入凭证（外部文件，不进入 git）
mkdir -p config/secrets config/mcp-server
echo "FEISHU_APP_ID=cli_xxx" > config/secrets/.env
echo "FEISHU_APP_SECRET=xxx" >> config/secrets/.env
echo "LLM_API_KEY=sk-xxx" >> config/secrets/.env

# 3. 启动
docker compose up --build -d

# 4. 检查状态
docker compose ps
curl http://localhost:8000/health
```

### 目录结构（含挂载点）

```
/opt/feishu-bot-service/       ← 代码（git pull）
├── config/
│   ├── secrets/               ← 🔒 敏感配置（不提交）
│   │   └── .env               ← 飞书 + LLM 凭证
│   ├── mcp-server/            ← 🧩 MCP Server 配置（可选）
│   │   └── mcp.yaml           ← SSE endpoints + 认证头
│   └── system_prompt.md       ← 🗣️ 人设/系统提示词（git 跟踪，Markdown）
├── main.py                    # FastAPI 入口
├── bot/                       # 飞书 Bot 核心逻辑
├── llm/                       # LLM 客户端
├── mcp_plugin/                # MCP 插件层（SSE Transport；改名自 mcp/ 以免与 pip 的 mcp SDK 撞包名）
├── utils/                     # 工具函数
├── tests/                     # 单元测试
├── Dockerfile                 # 容器化构建
├── docker-compose.yml         # 一键部署 + 卷挂载
└── .env.example               # 环境变量模板
```

### 更新 MCP / API Key

编辑 `config/secrets/.env` 后执行：

```bash
docker compose up -d   # 热重载环境变量
```

编辑 `config/mcp-server/mcp.yaml` 或 `config/system_prompt.md`（人设/系统提示词，Markdown 文档，直接改不用管 YAML 缩进转义）后执行：

```bash
docker compose restart feishu-bot
```

## 配置说明

### config.yaml 核心字段

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `feishu.app_id` | 飞书应用 ID | `${FEISHU_APP_ID}` |
| `feishu.app_secret` | 飞书应用密钥 | `${FEISHU_APP_SECRET}` |
| `feishu.websocket.enabled` | 是否启用 WebSocket | `true` |
| `llm.model` | LLM 模型名称 | `qwen-plus` |
| `llm.base_url` | OpenAI 兼容 Base URL | 通义千问 dashscope |
| `bot.trigger_mode` | 触发方式 | `at_or_direct` |
| `bot.response_scope` | 响应范围 | `group_and_dm` |

### 支持的 LLM 提供商

只需修改 `base_url` 和 `model`：

| 提供商 | base_url | model |
|--------|----------|-------|
| **通义千问** | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| **智谱** | `https://open.bigmodel.cn/api/paas/v4/` | `glm-4` |
| **DeepSeek** | `https://api.deepseek.com/v1` | `deepseek-chat` |
| **OpenAI** | `https://api.openai.com/v1` | `gpt-4o-mini` |

## API 文档

启动后访问 http://localhost:8000/docs 查看 Swagger UI。

- `GET /health` — 健康检查
- `POST /reloader` — (待实现) 热更新配置

## MCP 插件集成（可选）

飞书机器人支持通过 **SSE transport** 连接外部 [MCP Server](https://modelcontextprotocol.io/)，
让用户消息自动调用工具、查询资源。

### 工作原理

```
用户发消息 → Handler 匹配最合适的 Tool → MCP Client 执行 → 结果注入 LLM 上下文 → AI 回复
```

### 配置示例

在 `config.yaml` 的 `mcp` 段添加 Server：

```yaml
mcp:
  enabled: true                    # 必须设为 true
  servers:
    - name: "tavily-search"
      url: "http://localhost:8000/sse"   # MCP Server SSE endpoint
      headers: {}                          # 可选，如认证头
      timeout: 5.0                         # 连接超时(秒)
```

### 支持的 MCP Server

任何兼容 MCP 协议的 SSE Server 均可接入：

| Server | 用途 |
|--------|------|
| [Tavily](https://github.com/nicepkg/tavily-mcp) | Web 搜索 |
| [GitHub MCP](https://github.com/github/github-mcp-server) | GitHub 操作 |
| [Notion MCP](https://github.com/makenotion/notion-mcp) | Notion 文档管理 |
| [Linear MCP](https://linear.app/integrations/mcp) | Linear 项目管理 |

### 开发自定义 Tool

参考 [MCP Spec](https://modelcontextprotocol.io/specification/concepts/tools) 定义你的 Server，
通过 HTTP POST + SSE Event Stream 暴露 Tools。

## 项目结构

```
feishu-bot-service/
├── main.py              # FastAPI 入口，启动所有组件
├── bot/                 # 飞书 Bot 核心逻辑
│   ├── __init__.py
│   ├── engine.py        # BotEngine: WebSocket 监听 + HTTP Client
│   ├── handler.py       # MessageHandler: 消息处理管道（含 MCP Tool 调用）
│   └── context.py       # ConversationContext: 对话上下文
├── llm/                 # LLM 客户端
│   ├── __init__.py
│   └── client.py        # LLMClient + OpenAIClient
├── mcp_plugin/          # MCP 插件层（SSE Transport；曾用名 mcp/，与 pip 的 mcp SDK 撞包名已改名）
│   ├── __init__.py
│   ├── client.py        # MCPClient — SSE transport 连接
│   ├── manager.py       # MCPManager — 多 Server 管理
│   └── tools.py         # ToolRouter — 语义匹配工具路由
├── utils/               # 工具函数
│   ├── __init__.py
│   └── config.py        # 配置加载 (${VAR} 替换)
├── tests/               # 单元测试
│   └── test_bot.py
├── config.yaml          # 默认配置（含 mcp_servers 示例）
├── requirements.txt     # Python 依赖
├── Dockerfile           # 容器化构建
├── docker-compose.yml   # 一键部署
└── .env.example         # 环境变量模板
```
