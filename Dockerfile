FROM python:3.12-slim

WORKDIR /app

# 安装系统依赖
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl tini && \
    rm -rf /var/lib/apt/lists/*

# 复制代码（不含敏感配置）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 创建外部配置挂载目录
RUN mkdir -p /etc/feishu-bot/secrets /etc/feishu-bot/mcp-server

ENV PYTHONUNBUFFERED=1
ENV HOST=0.0.0.0
ENV PORT=8000

# 使用 tini 作为 init 进程，支持优雅信号处理
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "main.py"]

EXPOSE 8000
