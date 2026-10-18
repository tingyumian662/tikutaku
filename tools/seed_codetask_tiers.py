"""三源（D4J/BIP/Perf）独立难度尺播种 + LCB unrated 归档（2026-10-17 用户批准）。

三源规则（每源独立 3 档，不与 LCB 共尺——方法论红线）：
  L1 = pass 且 completion_tokens ≤ 该源 pass 中位数（快 pass）
  L2 = pass 且 token > 中位数（慢 pass）
  L3 = 有效失败（wrong_answer/compile_error/apply_fail/insufficient_speedup/timeout）
  tier_rank：L1/L2 档内按 token 升序 [0,1)；L3 不定义
  范围：D4J=有有效判定的全部；BIP=bip_pandas 视图（其余 repo 未标定不入池）；Perf=有有效判定
  retired 一律跳过。

LCB 归档：unrated 题（补测已出判定）按既有 (initial_difficulty, verdict) 规则回填：
  过: easy→L1 / medium→L2 / hard→L3；败: easy→L3 / medium→L4 / hard→L5
  随后全量重播种 LCB tier_rank。
用法: D:/tikutaku/venv/Scripts/python.exe tools/seed_codetask_tiers.py
"""
from __future__ import annotations

import json
import glob
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent

VIEW = {
    "bugsinpy": set(json.loads((ROOT / "bank/views/bip_pandas.json").read_text(encoding="utf-8"))) |
                set(json.loads((ROOT / "bank/views/bip_rest.json").read_text(encoding="utf-8"))),
}
VALID_FAIL = {"wrong_answer", "compile_error", "apply_fail", "insufficient_speedup", "timeout"}
LCB_FOLD = {("easy", "pass"): "L1", ("medium", "pass"): "L2", ("hard", "pass"): "L3",
            ("easy", "fail"): "L3", ("medium", "fail"): "L4", ("hard", "fail"): "L5"}


def latest_verdicts() -> dict[str, tuple[str, float]]:
    out: dict[str, tuple[str, float]] = {}
    for f in sorted(ROOT.glob("results/run-*/results.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            v = (r.get("score") or {}).get("verdict", "")
            if not v:
                continue
            out[r["bank_id"]] = (v, (r.get("usage") or {}).get("completion_tokens") or 0)
    return out


def main() -> int:
    lat = latest_verdicts()
    df = pd.read_parquet(ROOT / "bank" / "index.parquet")

    # ---- 三源 3 档 ----
    for src in ["defects4j", "bugsinpy", "sweperf"]:
        m = (df.source == src) & ~df.retired.astype(bool)
        if src == "bugsinpy":
            m &= df.bank_id.isin(VIEW["bugsinpy"])
        rows = []
        for _, r in df[m].iterrows():
            v, tok = lat.get(r.bank_id, ("", 0))
            if v == "pass" and tok:
                rows.append((r.bank_id, "pass", tok))
            elif v in VALID_FAIL:
                rows.append((r.bank_id, "fail", tok))
        toks = sorted(t for _, k, t in rows if k == "pass")
        med = toks[len(toks) // 2] if toks else 0
        for bid, kind, tok in rows:
            df.loc[df.bank_id == bid, "level"] = ("L1" if tok <= med else "L2") if kind == "pass" else "L3"
        # tier_rank：L1/L2 档内按 token 升序
        for lv in ("L1", "L2"):
            pool = [b for b, k, t in rows if (k == "pass") and
                    (("L1" if t <= med else "L2") == lv)]
            pool.sort(key=lambda b: dict((b, t) for _, k, t in rows if k == "pass")[b])
            for i, b in enumerate(pool):
                df.loc[df.bank_id == b, "tier_rank"] = round(i / max(len(pool), 1), 4)
        n_fail = sum(1 for _, k, _ in rows if k == "fail")
        print(f"{src}: 播种 {len(rows)} 题（pass→L1/L2 按中位 token {med}，fail→L3 {n_fail}）")

    # ---- LCB unrated 归档 + 重播种 ----
    m = (df.source == "livecodebench") & (df.level == "unrated")
    folded = 0
    for _, r in df[m].iterrows():
        v, _ = lat.get(r.bank_id, ("", 0))
        key = (str(r.initial_difficulty or "hard").lower(),
               "pass" if v == "pass" else ("fail" if v in VALID_FAIL else None))
        lv = LCB_FOLD.get(key)  # type: ignore[arg-type]
        if lv:
            df.loc[df.bank_id == r.bank_id, "level"] = lv
            folded += 1
    n_rank = 0
    for lv in ["L1", "L2", "L3"]:
        mm = (df.source == "livecodebench") & (df.level == lv)
        pool = [b for b in df.loc[mm, "bank_id"] if lat.get(b, ("", 0))[0] == "pass"]
        pool.sort(key=lambda b: lat[b][1])
        for i, b in enumerate(pool):
            df.loc[df.bank_id == b, "tier_rank"] = round(i / max(len(pool), 1), 4)
        n_rank += len(pool)
    print(f"livecodebench: unrated 归档 {folded} 题；tier_rank 重播种 {n_rank} 题")

    df.to_parquet(ROOT / "bank" / "index.parquet")
    print("已写入 bank/index.parquet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
