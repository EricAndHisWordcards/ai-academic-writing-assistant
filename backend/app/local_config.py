"""本地配置管理：让用户填写自己的 LLM API Key 等，持久化到本地文件。

配置文件路径：~/.academic_writer/config.json
优先级：环境变量 > 本地配置文件 > 默认值
"""
from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_DIR = Path.home() / ".academic_writer"
CONFIG_FILE = CONFIG_DIR / "config.json"

_DEFAULTS = {
    "llm_api_key": "",
    "llm_base_url": "",
    "llm_model": "deepseek-chat",
}


def _load() -> dict:
    """读取本地配置，文件不存在返回默认值。"""
    if not CONFIG_FILE.exists():
        return dict(_DEFAULTS)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {**_DEFAULTS, **data}
    except (json.JSONDecodeError, OSError):
        return dict(_DEFAULTS)


def get_local_llm_config() -> dict:
    """获取本地 LLM 配置（供 config.py 兜底使用）。"""
    return _load()


def save_local_llm_config(cfg: dict) -> dict:
    """保存本地 LLM 配置，返回保存后的完整配置。"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    allowed = {"llm_api_key", "llm_base_url", "llm_model"}
    to_save = {k: v for k, v in cfg.items() if k in allowed}
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(to_save, f, ensure_ascii=False, indent=2)
    return _load()
