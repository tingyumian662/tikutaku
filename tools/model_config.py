"""模型接入配置：GLM 为主力评测模型。

密钥不落库：运行时直接从 ~/.claude/settings.json 提取（用户指定来源）。
bigmodel 同一密钥兼容两种协议：
  OpenAI 兼容:  https://open.bigmodel.cn/api/paas/v4     （harness 用这个，openai SDK）
  Anthropic 兼容: settings 里的 /api/anthropic            （Claude Code 用这个）
"""
from __future__ import annotations

import json
import os
from pathlib import Path

SETTINGS = Path.home() / ".claude" / "settings.json"


def glm_settings() -> dict:
    env = json.loads(SETTINGS.read_text(encoding="utf-8")).get("env", {})
    return env


def glm_openai_config() -> tuple[str, str]:
    """(base_url, api_key) 供 OpenAI 协议使用。"""
    env = glm_settings()
    token = env.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ZAI_API_KEY", "")
    base = "https://open.bigmodel.cn/api/paas/v4"
    return base, token


GLM_MODEL = "glm-5.3"          # 主力
GLM_MODEL_FLASH = "glm-5.3-flash"  # 备用/便宜档
