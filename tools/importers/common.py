"""导入器公共工具：读写 parquet/jsonl、bank_id 分配、源登记。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from env import DATASETS, INDEX  # noqa: E402

# 源登记表：source key -> (前缀, task_type 默认, 许可, 署名要求)
SOURCES = {
    "swebench_verified":  ("SWBV",  "bug_fix", "MIT", "SWE-bench (Princeton NLP)"),
    "swebench_full":      ("SWBF",  "bug_fix", "MIT", "SWE-bench (Princeton NLP)"),
    "swebench_lite":      ("SWBL",  "bug_fix", "MIT", "SWE-bench (Princeton NLP)"),
    "swebench_multimodal":("SWBM",  "bug_fix", "MIT", "SWE-bench (Princeton NLP)"),
    "swelive_py":         ("SWLV",  "bug_fix", "MIT", "SWE-bench-Live (Microsoft)"),
    "swelive_multilang":  ("SWLM",  "bug_fix", "MIT", "SWE-bench-Live (Microsoft)"),
    "swelive_windows":    ("SWLW",  "bug_fix", "MIT", "SWE-bench-Live (Microsoft)"),
    "swerebench_v1":      ("SWR1",  "bug_fix", "CC-BY-4.0", "SWE-rebench (Nebius)"),
    "swerebench_v2":      ("SWR2",  "bug_fix", "CC-BY-4.0", "SWE-rebench (Nebius)"),
    "multiswebench":      ("MSB",   "bug_fix", "CC0", "Multi-SWE-bench (ByteDance Seed)"),
    "multiswebench_mini": ("MSBM",  "bug_fix", "CC0", "Multi-SWE-bench (ByteDance Seed)"),
    "multiswebench_flash":("MSBF",  "bug_fix", "CC0", "Multi-SWE-bench (ByteDance Seed)"),
    "swesmith":           ("SWSM",  "bug_fix", "MIT", "SWE-smith (SWE-bench)"),
    "r2egym_lite":        ("R2EG",  "bug_fix", "未标注(仅内部)", "R2E-Gym"),
    "terminalbench2":     ("TB2",   "terminal", "Apache-2.0", "Terminal-Bench (Harbor)"),
    "terminalbench1":     ("TB1",   "terminal", "Apache-2.0", "Terminal-Bench (Laude Institute)"),
    "livecodebench":      ("LCB",   "algo",    "CC-BY-4.0(题面)", "LiveCodeBench"),
    "sweperf":            ("SPF",   "perf",    "Apache-2.0", "SWE-Perf"),
    "cvebench":           ("CVEB",  "security", "Apache-2.0", "CVE-Bench (UIUC Kang Lab)"),
    "defects4j":          ("D4J",   "bug_fix", "MIT", "Defects4J"),
    "bugsinpy":           ("BIP",   "bug_fix", "未标注(仅内部)", "BugsInPy"),
    "swelancer":          ("SWL",   "feature", "MIT", "SWE-Lancer (OpenAI)"),
}


def bank_id(source: str, seq: int) -> str:
    return f"{SOURCES[source][0]}-{seq:06d}"


def read_any(path: Path) -> pd.DataFrame:
    """读 HF 数据集目录下的 parquet/jsonl/json 为 DataFrame。

    过滤下载器的元数据垃圾（.metadata、隐藏目录、根目录散装 json），
    并附加 _src_file 列（导入器可据此识别 split）。
    """
    def usable(p: Path) -> bool:
        rel = p.relative_to(path)
        parts = rel.parts
        if any(part.startswith(".") for part in parts):
            return False                       # .cache/.huggingface/隐藏目录
        if p.suffix == ".metadata":
            return False
        if p.suffix == ".json":
            # 只认 data/ 目录下或明确数据命名的 json，其余（卡片缓存等）不读
            stem = p.stem.lower()
            if "data" not in parts and stem not in ("test", "test2", "test3", "test4", "dev", "train", "validation", "data"):
                return False
        return True

    files = sorted(p for p in path.rglob("*")
                   if p.is_file() and p.suffix in (".parquet", ".jsonl", ".json") and usable(p))
    frames = []
    for p in files:
        if p.suffix == ".parquet":
            f = pd.read_parquet(p)
        elif p.suffix == ".jsonl":
            f = pd.read_json(p, lines=True)
        else:
            f = pd.read_json(p)
        f["_src_file"] = p.name
        frames.append(f)
    if not frames:
        raise FileNotFoundError(f"{path} 下没有可用数据文件")
    return pd.concat(frames, ignore_index=True)


def load_index() -> pd.DataFrame:
    return pd.read_parquet(INDEX) if INDEX.exists() else pd.DataFrame()


def save_index(df: pd.DataFrame) -> None:
    INDEX.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(INDEX, index=False)
    print(f"索引已写入: {INDEX} ({len(df)} 行)")


def jdump(x) -> str:
    return json.dumps(x, ensure_ascii=False) if x is not None else "[]"


GOLD_KEYS = ("patch", "fix_patch", "gold_patch", "parsed_commit_content", "solution")


def origin_from_row(row, drop: tuple = GOLD_KEYS) -> dict:
    """源行完整序列化为 origin（剔除金标字段，防泄漏）。"""
    import numpy as np

    def norm(v):
        if isinstance(v, np.ndarray):
            v = v.tolist()
        if isinstance(v, np.generic):
            v = v.item()
        return v

    out = {}
    for k, v in row.items():
        if k in drop or k == "_src_file":
            continue
        try:
            v = norm(v)
            if v is None:
                continue
            json.dumps(v, ensure_ascii=False)   # 可序列化才收
            out[k] = v
        except (TypeError, ValueError):
            out[k] = str(v)[:20000]
    return out
