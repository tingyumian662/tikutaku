"""SWE-Perf 金标封存：patch + test_patch + 效率元数据打包 seal。

用法: D:/tikutaku/venv/Scripts/python.exe tools/seal_perf.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from seal_gold import seal, unseal  # noqa: E402


def main() -> int:
    import pandas as pd

    pq = pd.read_parquet(ROOT / "sources/datasets/swe-perf/data/test-00000-of-00001.parquet")
    idx = pd.read_parquet(ROOT / "bank/index.parquet")
    perf = idx[idx.source == "sweperf"]
    print(f"index {len(perf)} 题 | parquet {len(pq)} 行")
    by_id = {r.instance_id: r for _, r in pq.iterrows()}
    n_ok = n_skip = n_mismatch = 0
    for _, r in perf.iterrows():
        row = by_id.get(r.source_id)
        if row is None:
            n_mismatch += 1
            continue
        blob = {
            "patch": row.patch,
            "test_patch": row.test_patch,
            "efficiency_test": list(row.efficiency_test),
            "duration_changes": row.duration_changes,
            "human_performance": float(row.human_performance) if row.human_performance == row.human_performance else None,
            "base_commit": row.base_commit,
            "head_commit": row.head_commit,
            "version": row.version,
            "patch_functions": row.patch_functions,
        }
        seal(r.bank_id, json.dumps(blob, ensure_ascii=False))
        n_ok += 1
    print(f"seal {n_ok} | parquet 无对应 {n_mismatch}")
    # 抽 1 题验证可解
    sample = perf.iloc[0].bank_id
    back = json.loads(unseal(sample))
    assert back["patch"].startswith("diff --git"), sample
    print(f"可解性验证 OK: {sample} | patch {len(back['patch'])} 字符")
    return 0


if __name__ == "__main__":
    sys.exit(main())
