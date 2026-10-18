"""统一题目 schema（bank/index.parquet 每行一题）。

七组字段：
  identity    题目身份
  timing      时间锚点（防污染核心）
  content     题面内容
  verifier    判分依据
  environment 运行环境
  calibration 难度标定（试测后回填）
  contamination 污染标记
"""
from __future__ import annotations

import datetime as _dt

TASK_TYPES = ("bug_fix", "perf", "security", "algo", "terminal", "feature", "refac")
VERIFIER_TYPES = ("f2p_p2p_tests", "efficiency_test", "oj_tests", "reward_script", "endtoend")
ENV_TYPES = ("docker_image", "docker_build", "conda", "venv", "none")
LEVELS = ("L1", "L2", "L3", "L4", "L5", "unknown")

COLUMNS: dict[str, str] = {
    # identity
    "bank_id": "str",
    "source": "str",
    "source_id": "str",
    "source_url": "str",
    # timing
    "created_at": "str",       # ISO 日期：issue/PR 创建日（晚于模型 cutoff 则对它防污染）
    "imported_at": "str",
    # content
    "language": "str",
    "repo": "str",
    "task_type": "str",
    "problem_statement": "str",  # 存任务目录的 task.json 里，索引里只留摘要
    "gold_seal": "str",          # seals/<bank_id>.bin 引用
    # verifier
    "verifier_type": "str",
    "fail_to_pass": "str",       # JSON 数组序列化（parquet 单元格里存 list 也可，序列化更稳）
    "pass_to_pass": "str",
    "timeout_sec": "int64",
    # environment
    "env_type": "str",
    "image_ref": "str",
    "image_registry": "str",
    "env_notes": "str",
    # calibration
    "initial_difficulty": "str",
    "level": "str",
    "empirical_rate": "float64",
    "retired": "bool",
    # contamination
    "cutoff_ok_json": "str",     # {model: bool} 序列化，cutoff_filter 生成
    "notes": "str",
}

DTYPES = {
    "timeout_sec": "int64",
    "empirical_rate": "float64",
    "retired": "bool",
}


def now_iso() -> str:
    return _dt.datetime.now().replace(microsecond=0).isoformat()


def new_task(**kw) -> dict:
    """造一条合法记录：必填字段缺省补齐。"""
    base = {
        "bank_id": "", "source": "", "source_id": "", "source_url": "",
        "created_at": "", "imported_at": now_iso(),
        "language": "", "repo": "", "task_type": "bug_fix",
        "problem_statement": "", "gold_seal": "",
        "verifier_type": "f2p_p2p_tests", "fail_to_pass": "[]", "pass_to_pass": "[]",
        "timeout_sec": 1800,
        "env_type": "none", "image_ref": "", "image_registry": "", "env_notes": "",
        "initial_difficulty": "unknown", "level": "unknown",
        "empirical_rate": float("nan"), "retired": False,
        "cutoff_ok_json": "", "notes": "",
    }
    base.update(kw)
    return base
