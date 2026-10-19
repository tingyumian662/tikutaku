"""锚卷多模型重切难度档（dry-run 默认 / --apply 带备份）。

攒够 3+ 模型锚卷成绩后，把"单模型 GLM 标定尺"换成"多模型通过率尺"：
每题取各模型最新一轮锚卷判定 → 多模型通过率 → 域内分位数切档（分域独立尺，
algo 五档 / 三源三档）→ dry-run 出建议表，人工确认后 --apply 改 index.parquet。

语义提示：三源旧尺是"快pass/慢pass/失败"（单模型行为尺），重切后是
"多模型通过率尺"——两把尺都有效，RUNBOOK 有对照说明。只动锚卷涉及的题
（锚卷是标定器，不做全库重切）；--apply 后需重启 webapp（池有模块级缓存）。

用法:
  uv run python tools/recalibrate_from_anchor.py --anchor anchor-v1            # dry-run
  uv run python tools/recalibrate_from_anchor.py --anchor anchor-v1 --apply    # 备份后落盘
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "tools"))

import runner_core as rc  # noqa: E402
from env import BANK, INDEX, LOGS, RESULTS  # noqa: E402

# 域 → 档数（切位 = 均匀分位数；L1 最易 … L5 最难）
DOMAIN_TIERS = {"algo": 5, "java_fix": 3, "py_fix": 3, "perf": 3}


def collect(anchor_id: str) -> dict[str, dict]:
    """每题跨模型最新判定：{bank_id: {model: verdict}}（作废 error 不计）。"""
    doc = rc.load_anchor(anchor_id)
    ids = set(doc["bank_ids"])
    # 每模型最新锚卷 run（按 run 目录时间序，后者覆盖前者）
    latest: dict[str, dict[str, str]] = {}  # model -> {bank_id: verdict}
    for d in sorted(RESULTS.glob("run-*")):
        cf = d / "config.json"
        if not cf.exists():
            continue
        try:
            cfg = json.loads(cf.read_text(encoding="utf-8"))
        except Exception:
            continue
        if (cfg.get("anchor_id") or cfg.get("anchor")) != anchor_id:
            continue
        f = d / "results.jsonl"
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("bank_id") not in ids:
                continue
            v = (r.get("score") or {}).get("verdict", "?")
            if v.split(":")[0] == "error":
                continue  # 作废测量不进重切
            latest.setdefault(r.get("model", "?"), {})[r.get("bank_id")] = v
    # 展开为题维度
    per_task: dict[str, dict] = {}
    for model, verdicts in latest.items():
        for bid, v in verdicts.items():
            per_task.setdefault(bid, {})[model] = v
    return per_task


def plan(anchor_id: str, min_models: int) -> dict:
    """重切计划：域内按多模型通过率分位切档。"""
    doc = rc.load_anchor(anchor_id)
    snap = doc.get("per_task") or {}
    per_task = collect(anchor_id)
    eligible = {bid: vs for bid, vs in per_task.items() if len(vs) >= min_models}
    models = sorted({m for vs in per_task.values() for m in vs})

    import pandas as pd
    df = pd.read_parquet(INDEX, columns=["bank_id", "level"])
    cur_level = dict(zip(df["bank_id"], df["level"]))

    changes = []
    kept: dict[str, int] = {}
    for domain, n_tier in DOMAIN_TIERS.items():
        dom_ids = [b for b, info in snap.items() if info.get("domain") == domain]
        items = []
        for bid in dom_ids:
            vs = eligible.get(bid)
            if not vs:
                continue
            n_pass = sum(1 for v in vs.values() if v == "pass")
            items.append({"bank_id": bid, "rate": n_pass / len(vs), "n_models": len(vs)})
        items.sort(key=lambda x: (-x["rate"], x["bank_id"]))  # 易→难
        n = len(items)
        for i, it in enumerate(items):
            # 均匀分位：i/n 落在第几个 1/n_tier 区间；同分位并列时顺序稳定（按 bank_id）
            pos = min(n_tier - 1, int(i * n_tier / n)) if n else 0
            new_level = f"L{pos + 1}"  # pos 0 = 最易 = L1
            old = str(cur_level.get(it["bank_id"], "") or "?")
            row = {"bank_id": it["bank_id"], "domain": domain, "cur": old,
                   "new": new_level, "rate": round(it["rate"], 3), "n_models": it["n_models"],
                   "tier_rank": round(i / n, 4) if n else 0.0}  # 0=域内最易
            if new_level != old:
                changes.append(row)
            else:
                kept[domain] = kept.get(domain, 0) + 1
    return {"anchor_id": anchor_id, "min_models": min_models, "models": models,
            "n_eligible": len(eligible), "n_total_tasks": len(doc["bank_ids"]),
            "changes": changes, "kept": kept,
            "per_domain": {d: sum(1 for b, i in snap.items() if i.get("domain") == d)
                           for d in DOMAIN_TIERS}}


def apply(p: dict) -> str:
    """备份 index.parquet → 改参与题 level/tier_rank → 落盘 + 清单留档。返回备份路径。"""
    import pandas as pd
    ts = time.strftime("%Y%m%d-%H%M%S")
    backups = BANK / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    backup = backups / f"index-{ts}.parquet"
    shutil.copy2(INDEX, backup)
    snap = rc.load_anchor(p["anchor_id"]).get("per_task") or {}
    df = pd.read_parquet(INDEX)
    lv = dict(zip(df["bank_id"], df["level"]))
    tr = dict(zip(df["bank_id"], df["tier_rank"])) if "tier_rank" in df.columns else {}
    touched = 0
    for row in p["changes"]:
        bid = row["bank_id"]
        lv[bid] = row["new"]
        tr[bid] = row["tier_rank"]
        touched += 1
    # kept（档位不变的参与题）也重播 tier_rank 以对齐新尺；锚卷外与数据不足的题不动
    df["level"] = [lv.get(b, cur) for b, cur in zip(df["bank_id"], df["level"])]
    if "tier_rank" in df.columns:
        df["tier_rank"] = [tr[b] if b in tr else cur for b, cur in zip(df["bank_id"], df["tier_rank"])]
    df.to_parquet(INDEX, index=False)
    manifest = {"ts": ts, **{k: v for k, v in p.items() if k != "changes"},
                "n_changed": touched, "backup": str(backup), "changes": p["changes"]}
    mf = LOGS / f"recalibrate_{ts}.json"
    mf.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已改 {touched} 题档位 | 备份: {backup} | 清单: {mf}")
    print("⚠ 重启 webapp 后生效（池与档位映射有模块级缓存）；回滚 = 用备份覆盖 index.parquet")
    return str(backup)


def main() -> int:
    ap = argparse.ArgumentParser(description="锚卷多模型重切难度档")
    ap.add_argument("--anchor", required=True)
    ap.add_argument("--min-models", type=int, default=3,
                    help="题至少被几个模型测过才参与重切（默认 3）")
    ap.add_argument("--apply", action="store_true", help="实际落盘（默认 dry-run）")
    args = ap.parse_args()

    p = plan(args.anchor, args.min_models)
    print(f"锚卷 {p['anchor_id']}：{p['n_total_tasks']} 题，参与模型 {p['models']}，"
          f"够 {args.min_models} 模型的题 {p['n_eligible']} 个")
    if p["n_eligible"] < 10:
        print("⚠ 参与题太少（<10）：换更多模型跑锚卷攒数据，本次不建议重切")
        if not p["changes"]:
            return 0
    if not p["changes"]:
        print("所有参与题档位与建议一致，无需改动。")
        kept_total = sum(p["kept"].values())
        print(f"（{kept_total} 题档位不变；tier_rank 仅在 --apply 时随新尺重播）")
        return 0
    print(f"\n建议变更 {len(p['changes'])} 题（域内多模型通过率分位切档，L1 易→L5 难）：")
    by_dom: dict[str, list] = {}
    for c in p["changes"]:
        by_dom.setdefault(c["domain"], []).append(c)
    for dom, rows in by_dom.items():
        print(f"\n[{dom}] {len(rows)} 题变化")
        for c in sorted(rows, key=lambda x: -x["rate"])[:12]:
            print(f"  {c['bank_id']}  {c['cur']}→{c['new']}  rate={c['rate']:.2f} ({c['n_models']}模型)")
        if len(rows) > 12:
            print(f"  … 共 {len(rows)} 题")
    if args.apply:
        apply(p)
    else:
        print("\n[dry-run] 确认无误后加 --apply 落盘（自动备份到 bank/backups/）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
