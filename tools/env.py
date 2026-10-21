"""统一环境配置：所有工具脚本 import 本模块加载运行环境。"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "sources"
DATASETS = SOURCES / "datasets"
REPOS = SOURCES / "repos"
BANK = ROOT / "bank"
INDEX = BANK / "index.parquet"
TASKS = BANK / "tasks"
SEALS = BANK / "seals"
VIEWS = BANK / "views"
RESULTS = ROOT / "results"
LOGS = ROOT / "logs"

# HuggingFace 主站不通，走镜像站；已登录 token 优先（gated 数据集用）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
# 镜像站不支持 Xet CAS 协议，必须禁用否则 snapshot_download 报 I/O error
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

# 受测模型知识截止日（cutoff_filter 用；新增模型在此登记）
MODEL_CUTOFFS = {
    "gpt-5.4": "2025-06",
    "gpt-6": "2025-10",
    "claude-opus-5": "2025-08",
    "claude-sonnet-5.5": "2026-01",
    "glm-5.3": "2025-12",
    "glm-5.3-flash": "2025-12",
    # GLM-5.5 发布后在此登记截止日，再跑 anchor-v1 同卷对比
    "deepseek-v4-pro": "2025-11",
    "gemini-3.5-pro": "2026-02",
}
