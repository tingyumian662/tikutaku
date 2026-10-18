"""level 播种：同档内以 completion_tokens（思考消耗）作难度连续序（2026-10-13 用户决策②）。

规则：每档（L1-L5）内，pass 题按 completion_tokens 升序 → tier_rank ∈ [0,1)（0=档内最易）。
非 pass / 无 usage 题不定义（NaN）。只动 LCB（oj_tests 域唯一有五档标定的源）；
D4J/BIP/Perf 为独立难度尺，不共用（方法论红线）。
用法: D:/tikutaku/venv/Scripts/python.exe tools/seed_tier_rank.py
"""
from __future__ import annotations

import json
import glob
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent

# 最新有效记录（跨 run 后写覆盖）
latest = {}
for f in sorted(ROOT.glob("results/run-*/results.jsonl")):
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        v = (r.get("score") or {}).get("verdict", "")
        if str(r.get("source", "")) == "livecodebench" and v \
                and not str(v).startswith("error") and v != "empty_reply":
            latest[r["bank_id"]] = (v, (r.get("usage") or {}).get("completion_tokens"))

df = pd.read_parquet(ROOT / "bank" / "index.parquet")
df["tier_rank"] = float("nan")
n = 0
for lv in ["L1", "L2", "L3", "L4", "L5"]:
    m = (df.source == "livecodebench") & (df.level == lv)
    toks = {b: t for b, (v, t) in latest.items() if v == "pass" and t}
    rows = [b for b in df.loc[m, "bank_id"] if b in toks]
    rows.sort(key=lambda b: toks[b])
    for i, b in enumerate(rows):
        df.loc[df.bank_id == b, "tier_rank"] = round(i / max(len(rows), 1), 4)
    n += len(rows)
    print(f"{lv}: {int(m.sum())} 题，pass 有 token {len(rows)} 题，rank 已播种")

df.to_parquet(ROOT / "bank" / "index.parquet")
print(f"合计播种 {n} 题 → bank/index.parquet 新增 tier_rank 列")
