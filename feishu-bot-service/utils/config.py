"""配置加载模块。

优先级：config.yaml → config.local.yaml（被 .gitignore 排除）→ 环境变量
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


def load_config(config_path: str | None = None) -> dict:
    """加载并解析配置文件。

    优先使用 CONFIG_FILE 环境变量指定的路径，否则依次尝试
    ``config.local.yaml`` → ``config.yaml``。
    """
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE

    path = Path(config_path) if config_path else None
    candidates = []

    if path:
        candidates.append(Path(path).expanduser())
    else:
        candidates.extend([
            Path(__file__).resolve().parent.parent / "config.local.yaml",
            Path(__file__).resolve().parent.parent / "config.yaml",
        ])

    merged: dict = {}
    loaded = False
    for p in candidates:
        if p.exists():
            logger.info("✅ 加载配置文件: {}", p)
            with open(p, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if isinstance(data, dict):
                merged.update(data)
            loaded = True
        else:
            logger.debug("跳过不存在的配置文件: {}", p)

    if not loaded:
        raise FileNotFoundError(
            "未找到配置文件。请复制 config.yaml 到 config.local.yaml 并填入你的凭证。"
        )

    _CONFIG_CACHE = _walk(merged)
    return _CONFIG_CACHE


# convenience — 调用 get_config() 即可
get_config = load_config


def clear_cache() -> None:
    """清除配置缓存，重新加载。用于测试。"""
    global _CONFIG_CACHE
    _CONFIG_CACHE = None
