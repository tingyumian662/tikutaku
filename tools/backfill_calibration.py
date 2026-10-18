"""标定回填：从 results/run-*/results.jsonl 汇总各题实测结果 → 回填索引。

  empirical_rate = 通过次数 / 尝试次数（跨 run 累计）
  level 初版分层启发式（单模型种子，后续多模型/多次运行自动细化）：
    以源难度标签 × 实测结果组合定级：
      easy  pass→L1  fail→L3
      medium pass→L2  fail→L4
      hard  pass→L3  fail→L5
      其他源 pass→L2  fail→L4
用法: uv run python tools/backfill_calibration.py [--model glm-5.3]
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "importers"))


def collect(results_dir: Path, model: str | None) -> dict[str, dict]:
    """{bank_id: {"pass": n, "total": n}}"""
    stats: dict[str, dict] = defaultdict(lambda: {"pass": 0, "total": 0})
    for f in sorted(results_dir.glob("run-*/results.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if model and r.get("model") != model:
                continue
            bid = r.get("bank_id")
            if not bid:
                continue
            v = r.get("score", {}).get("verdict", "")
            if v in ("pass", "wrong_answer", "timeout", "compile_error"):
                stats[bid]["total"] += 1
                if v == "pass":
                    stats[bid]["pass"] += 1
    return stats


LEVEL_SEED = {
    ("easy", True): "L1", ("easy", False): "L3",
    ("medium", True): "L2", ("medium", False): "L4",
    ("hard", True): "L3", ("hard", False): "L5",
}


def main() -> int:
    model = sys.argv[sys.argv.index("--model") + 1] if "--model" in sys.argv else None
    from common import load_index, save_index
    from env import RESULTS

    stats = collect(RESULTS, model)
    df = load_index()
    hit = df["bank_id"].isin(stats.keys())
    print(f"汇总 {len(stats)} 题的实测数据（模型过滤: {model or '全部'}）")

    rates, levels = {}, {}
    for bid, s in stats.items():
        rate = s["pass"] / s["total"] if s["total"] else float("nan")
        rates[bid] = rate
        idx = df.index[df["bank_id"] == bid]
        if len(idx):
            diff = str(df.loc[idx[0], "initial_difficulty"]).lower()
            levels[bid] = LEVEL_SEED.get((diff, rate >= 0.5),
                                         "L2" if rate >= 0.5 else "L4")
    df.loc[hit, "empirical_rate"] = df.loc[hit, "bank_id"].map(rates)
    df.loc[hit, "level"] = df.loc[hit, "bank_id"].map(levels)
    save_index(df)

    done = df[df["empirical_rate"].notna()]
    print("\n== 分层结果 ==")
    print(done.groupby("level").size().to_string())
    print(f"\n通过率: {done['empirical_rate'].mean():.1%}（{len(done)} 题已标定）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
