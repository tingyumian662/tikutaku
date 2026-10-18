"""审计后重标：把审计结论落到数据层。

  1) 46 翻案题 → 最新记录 verdict 改 pass（note 标注审计翻案级别）
  2) 231 空回复题 → verdict 改 error:empty_reply（skip-done 不认 error，
     下次评测自动重测）；index 里清 empirical_rate、level 置 unrated（踢出组卷池）
  3) 重跑 backfill_calibration 逻辑，重算翻案后的 empirical_rate / level
用法: uv run python tools/recalibrate_after_audit.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tools" / "importers"))

from common import load_index, save_index  # noqa: E402
from env import RESULTS  # noqa: E402


def rewrite_results(flips: dict[str, str], empties: set[str]) -> tuple[int, int]:
    """就地改写 run-*/results.jsonl 中目标题的最新判定。返回 (翻案数, 作废数)。"""
    # 先找每题最新记录所在文件（时间序遍历，后见覆盖）
    latest: dict[str, Path] = {}
    for f in sorted(RESULTS.glob("run-*/results.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            bid = r.get("bank_id")
            if bid and (bid in flips or bid in empties):
                latest[bid] = f
    nflips = nempty = 0
    touched: dict[Path, list[str]] = {}
    for f in sorted(set(latest.values())):
        lines_out = []
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                lines_out.append(line)
                continue
            bid = r.get("bank_id", "")
            if latest.get(bid) == f and bid in flips:
                r.setdefault("score", {})["verdict"] = "pass"
                r["score"]["note"] = f"audit-flip:{flips[bid]}"
                r["score"]["passed"] = r["score"].get("total", 1)
                nflips += 1
            elif latest.get(bid) == f and bid in empties:
                r.setdefault("score", {})["verdict"] = "error:empty_reply"
                r["score"]["note"] = "思考链耗尽token无文本输出，测量无效待重测"
                nempty += 1
            lines_out.append(json.dumps(r, ensure_ascii=False, default=str))
        touched[f] = lines_out
    for f, lines in touched.items():
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return nflips, nempty


def invalidate_in_index(empties: set[str]) -> None:
    df = load_index()
    hit = df["bank_id"].isin(empties)
    df.loc[hit, "empirical_rate"] = float("nan")
    df.loc[hit, "level"] = "unrated"
    print(f"index: {hit.sum()} 题置 unrated（踢出组卷池，待重测回填）")
    save_index(df)


def main() -> int:
    audit = json.loads((ROOT / "logs" / "scoring_noise_audit.json").read_text(encoding="utf-8"))
    flips = {bid: lvl for bid, lvl in audit["flippable"]}
    empties = set(audit["empty_reply"])
    print(f"审计结论: 翻案 {len(flips)} 题, 空回复 {len(empties)} 题")

    nflips, nempty = rewrite_results(flips, empties)
    print(f"results.jsonl 改写: 翻案 {nflips}, 作废 {nempty}")
    invalidate_in_index(empties)

    # 重算（空回复题 verdict 已是 error，collect 不统计；翻案题按新 pass 计）
    import backfill_calibration as bc
    stats = bc.collect(RESULTS, "glm-5.3")
    df = load_index()
    hit = df["bank_id"].isin(stats.keys())
    rates, levels = {}, {}
    for bid, s in stats.items():
        rate = s["pass"] / s["total"] if s["total"] else float("nan")
        rates[bid] = rate
        idx = df.index[df["bank_id"] == bid]
        if len(idx):
            diff = str(df.loc[idx[0], "initial_difficulty"]).lower()
            levels[bid] = bc.LEVEL_SEED.get((diff, rate >= 0.5),
                                            "L2" if rate >= 0.5 else "L4")
    df.loc[hit, "empirical_rate"] = df.loc[hit, "bank_id"].map(rates)
    df.loc[hit, "level"] = df.loc[hit, "bank_id"].map(levels)
    save_index(df)

    print("\n== 重标后分层（livecodebench）==")
    lcb = df[(df["source"] == "livecodebench")]
    print(lcb.groupby("level").size().to_string())
    done = lcb[lcb["empirical_rate"].notna()]
    print(f"\n有效标定 {len(done)} 题 | 平均通过率 {done['empirical_rate'].mean():.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
