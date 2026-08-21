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

### 4. Docker 部署

```bash
docker compose up --build -d
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

## 项目结构

```
feishu-bot-service/
├── main.py              # FastAPI 入口，启动所有组件
├── bot/
│   ├── __init__.py
│   ├── engine.py        # BotEngine: WebSocket 监听 + HTTP Client
│   ├── handler.py       # MessageHandler: 消息处理管道
│   └── context.py       # ConversationContext: 对话上下文
├── llm/
│   ├── __init__.py
│   └── client.py        # LLMClient + OpenAIClient
├── utils/
│   ├── __init__.py
│   └── config.py        # 配置加载 (${VAR} 替换)
├── config.yaml          # 默认配置
├── requirements.txt     # Python 依赖
├── Dockerfile           # 容器化构建
├── docker-compose.yml   # 一键部署
└── .env.example         # 环境变量模板
```
