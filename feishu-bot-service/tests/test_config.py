"""utils/config.py 的分层合并测试。

覆盖两个曾经存在的真实 bug：
1. config.local.yaml 的覆盖被 config.yaml 自己的同名顶层字段整体清空
   （原实现是浅层 dict.update，且 config.yaml 后加载、后写覆盖前写）。
2. CONFIG_FILE 环境变量从未被 load_config() 读取，docker-compose.yml
   里挂载的 mcp.yaml 因此完全不生效。
"""

from __future__ import annotations

import textwrap

import pytest

from utils import config as config_module
from utils.config import load_config


@pytest.fixture(autouse=True)
def _clear_cache():
    config_module.clear_cache()
    yield
    config_module.clear_cache()


def _write(path, content: str) -> None:
    path.write_text(textwrap.dedent(content), encoding="utf-8")


def test_local_override_survives_deep_merge(tmp_path, monkeypatch):
    """config.local.yaml 只改了 llm.model，config.yaml 里 llm 段的其他
    字段（如 base_url）必须保留，不能被 config.yaml 自己的 llm 段整体
    覆盖回去——这是本次修的第一个 bug。"""
    base_dir = tmp_path
    _write(base_dir / "config.yaml", """\
        llm:
          model: "default-model"
          base_url: "https://default.example.com/v1"
        mcp:
          enabled: false
          servers: []
        """)
    _write(base_dir / "config.local.yaml", """\
        llm:
          model: "local-override-model"
        """)

    monkeypatch.setattr(config_module, "__file__", str(base_dir / "utils" / "config.py"))

    cfg = load_config()
    assert cfg["llm"]["model"] == "local-override-model"
    assert cfg["llm"]["base_url"] == "https://default.example.com/v1"
    assert cfg["mcp"]["enabled"] is False


def test_config_file_env_var_layers_on_top(tmp_path, monkeypatch):
    """CONFIG_FILE 指向一个只包含 mcp 段的文件时，必须真正生效并覆盖
    config.yaml 的 mcp 默认值，同时不影响 feishu/llm 等其他段——这是
    本次修的第二个 bug（CONFIG_FILE 此前从未被读取）。"""
    base_dir = tmp_path
    _write(base_dir / "config.yaml", """\
        feishu:
          app_id: "cli_default"
        mcp:
          enabled: false
          servers: []
        """)
    mcp_file = base_dir / "mcp.yaml"
    _write(mcp_file, """\
        mcp:
          enabled: true
          servers:
            - name: "tavily-search"
              url: "http://localhost:9000/sse"
        """)

    monkeypatch.setattr(config_module, "__file__", str(base_dir / "utils" / "config.py"))
    monkeypatch.setenv("CONFIG_FILE", str(mcp_file))

    cfg = load_config()
    assert cfg["feishu"]["app_id"] == "cli_default"  # untouched by the mcp-only layer
    assert cfg["mcp"]["enabled"] is True
    assert cfg["mcp"]["servers"][0]["name"] == "tavily-search"


def test_system_prompt_file_overrides_inline_yaml_value(tmp_path, monkeypatch):
    """config/system_prompt.md 存在时必须覆盖 config.yaml 里的
    llm.system_prompt——外挂这个文件就是为了不用在 YAML 多行块里改人设。"""
    base_dir = tmp_path
    _write(base_dir / "config.yaml", """\
        llm:
          model: "default-model"
          system_prompt: "占位符，应该被外挂文件覆盖"
        """)
    prompt_dir = base_dir / "config"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    (prompt_dir / "system_prompt.md").write_text(
        "你是新人设\n第二行\n", encoding="utf-8"
    )

    monkeypatch.setattr(config_module, "__file__", str(base_dir / "utils" / "config.py"))

    cfg = load_config()
    assert cfg["llm"]["system_prompt"] == "你是新人设\n第二行"
    assert cfg["llm"]["model"] == "default-model"  # 其他字段不受影响


def test_system_prompt_file_absent_falls_back_to_yaml_value(tmp_path, monkeypatch):
    """没有外挂文件时，照常使用 config.yaml 里写的值，不能报错或清空。"""
    base_dir = tmp_path
    _write(base_dir / "config.yaml", """\
        llm:
          system_prompt: "config.yaml 里的默认值"
        """)

    monkeypatch.setattr(config_module, "__file__", str(base_dir / "utils" / "config.py"))

    cfg = load_config()
    assert cfg["llm"]["system_prompt"] == "config.yaml 里的默认值"


def test_empty_system_prompt_file_does_not_override(tmp_path, monkeypatch):
    """外挂文件存在但是空文件（比如误清空）时，不应该用空字符串覆盖掉
    config.yaml 里原有的值——避免误删文件内容导致 Anna 突然失去人设。"""
    base_dir = tmp_path
    _write(base_dir / "config.yaml", """\
        llm:
          system_prompt: "config.yaml 里的默认值"
        """)
    prompt_dir = base_dir / "config"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    (prompt_dir / "system_prompt.md").write_text("   \n", encoding="utf-8")

    monkeypatch.setattr(config_module, "__file__", str(base_dir / "utils" / "config.py"))

    cfg = load_config()
    assert cfg["llm"]["system_prompt"] == "config.yaml 里的默认值"
