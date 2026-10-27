"""M6 标定：按三源尺同款规则给 swesmith_local 合成题切三档 + tier_rank 回填。

规则（seed_codetask_tiers 同款，独立成尺——每源独立尺红线）：
  L1 = pass 且 completion_tokens ≤ 本源 pass 中位数（快 pass）
  L2 = pass 且 token > 中位数（慢 pass）
  L3 = 有效失败（wrong_answer/compile_error/apply_fail/timeout）
  tier_rank：L1/L2 档内按 token 升序 [0,1)；L3 不定义
判定来源：results/run-*/results.jsonl 最新一轮 glm-5.3 标定跑。

用法: D:/tikutaku/venv/Scripts/python.exe tools/smith_calibrate.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smith_common as sc  # noqa: E402

from seed_codetask_tiers import VALID_FAIL, latest_verdicts  # noqa: E402


def main() -> int:
    import pandas as pd
    from env import INDEX

    lat = latest_verdicts()
    df = pd.read_parquet(INDEX)
    m = (df.source == sc.SOURCE_KEY) & ~df.retired.astype(bool)
    rows = []
    for bid in df.loc[m, "bank_id"]:
        v, tok = lat.get(bid, ("", 0))
        if v == "pass" and tok:
            rows.append((bid, "pass", tok))
        elif v in VALID_FAIL:
            rows.append((bid, "fail", tok))
    if not rows:
        print("无可用判定（先跑 glm-5.3 标定轮）")
        return 1
    toks = sorted(t for _, k, t in rows if k == "pass")
    med = toks[len(toks) // 2] if toks else 0
    for bid, kind, tok in rows:
        df.loc[df.bank_id == bid, "level"] = ("L1" if tok <= med else "L2") if kind == "pass" else "L3"
    for lv in ("L1", "L2"):
        pool = sorted((b for b, k, t in rows if k == "pass" and (("L1" if t <= med else "L2") == lv)),
                      key=lambda b: dict((b, t) for _, k, t in rows if k == "pass")[b])
        for i, b in enumerate(pool):
            df.loc[df.bank_id == b, "tier_rank"] = round(i / max(len(pool), 1), 4)
    df.to_parquet(INDEX, index=False)
    n = {"L1": 0, "L2": 0, "L3": 0}
    for bid, kind, tok in rows:
        n["L3" if kind == "fail" else ("L1" if tok <= med else "L2")] += 1
    print(f"swesmith_local 标定 {len(rows)} 题（pass 中位 token {med}）: {json.dumps(n)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
