"""配置加载模块。

分层叠加，后面的层覆盖前面的层（同名字段深合并，不是整段替换）：
    config.yaml（默认值，git 跟踪）
        → config.local.yaml（本地/部署覆盖，被 .gitignore 排除，可选）
        → CONFIG_FILE 环境变量指定的文件（最高优先级，可选 —
          例如只包含 `mcp:` 段的 config/mcp-server/mcp.yaml）
        → config/system_prompt.md（若存在，覆盖 llm.system_prompt——
          Markdown 文档，不用在 YAML 多行块里操心缩进/转义，方便直接改）
环境变量通过 ${VAR_NAME} 语法在 YAML 中引用。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from loguru import logger

_CONFIG_CACHE: dict | None = None

# 匹配 ${VAR_NAME} 或 ${VAR_NAME:-default}
_ENV_RE = re.compile(r"\$\{([^}:]+)(?::-([^}]*))?\}")

# system_prompt 外挂文件的固定位置，相对仓库根目录
_SYSTEM_PROMPT_RELPATH = Path("config") / "system_prompt.md"


def _resolve_env(value: str) -> str:
    """将字符串中的 ${VAR} 引用替换为对应的环境变量值。"""
    def _repl(m: re.Match) -> str:
        var, default = m.group(1), m.group(2)
        return os.environ.get(var, default or "")
    return _ENV_RE.sub(_repl, value)


def _walk(obj):
    """递归遍历 dict/list，将字符串中的 ${VAR} 替换为环境变量。"""
    if isinstance(obj, dict):
        return {k: _walk(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(v) for v in obj]
    if isinstance(obj, str):
        resolved = _resolve_env(obj)
        # 如果 resolve 后仍包含 $，保留原样供后续 env fallback
        return resolved
    return obj


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并两个 dict：``override`` 中的字段覆盖 ``base``，但嵌套 dict
    是逐 key 合并而非整段替换——``override`` 只写了 ``mcp.enabled``，
    ``base`` 里 ``mcp.servers`` 之类的其他 key 会保留，不会被整段清空。"""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(config_path: str | None = None) -> dict:
    """加载并分层合并配置文件。

    始终按顺序加载并深合并：``config.yaml``（默认值）→
    ``config.local.yaml``（若存在）→ ``config_path`` 参数指定的文件，
    若未传参数则改用 ``CONFIG_FILE`` 环境变量指定的路径（若设置且文件
    存在）。每一层只需要写自己关心的字段——不需要在
    ``config/mcp-server/mcp.yaml`` 里重复整份 ``feishu``/``llm`` 配置，
    只写 ``mcp:`` 段即可覆盖 ``config.yaml`` 里的默认值。
    """
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE

    base_dir = Path(__file__).resolve().parent.parent
    candidates = [base_dir / "config.yaml", base_dir / "config.local.yaml"]

    extra_path = config_path or os.environ.get("CONFIG_FILE")
    if extra_path:
        candidates.append(Path(extra_path).expanduser())

    merged: dict = {}
    loaded = False
    for p in candidates:
        if p.exists():
            logger.info("✅ 加载配置文件: {}", p)
            with open(p, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if isinstance(data, dict):
                merged = _deep_merge(merged, data)
            loaded = True
        else:
            logger.debug("跳过不存在的配置文件: {}", p)

    if not loaded:
        raise FileNotFoundError(
            "未找到配置文件。请复制 config.yaml 到 config.local.yaml 并填入你的凭证。"
        )

    prompt_path = base_dir / _SYSTEM_PROMPT_RELPATH
    if prompt_path.exists():
        prompt_text = prompt_path.read_text(encoding="utf-8").strip()
        if prompt_text:
            merged.setdefault("llm", {})["system_prompt"] = prompt_text
            logger.info("✅ 加载外挂 system_prompt: {}", prompt_path)

    _CONFIG_CACHE = _walk(merged)
    return _CONFIG_CACHE


# convenience — 调用 get_config() 即可
get_config = load_config


def clear_cache() -> None:
    """清除配置缓存，重新加载。用于测试。"""
    global _CONFIG_CACHE
    _CONFIG_CACHE = None
