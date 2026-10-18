"""金标预检：拉下来的题先验证"金标 patch 能让 FAIL_TO_PASS 通过"再入正式测试队列。

分层实现（按 verifier_type / source）：
  f2p_p2p_tests + swebench 系  → 交官方 swebench harness 跑 gold（最准，"对表"）
  oj_tests (LiveCodeBench)     → 跳过（OJ 测试自证）
  其他                          → 标记 audit_pending，待对应 harness 适配后补检

用法:
  uv run python tools/audit_task.py --report logs/last_pull_report.json
  uv run python tools/audit_task.py --bank-ids SWBV-000001,SWBV-000002
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def gold_predictions(bank_ids: list[str], out: Path) -> int:
    """把选中 swebench 系题目的金标解开成 predictions jsonl（临时文件，检完即删）。"""
    import pandas as pd
    from common import load_index
    from seal_gold import unseal

    df = load_index()
    rows = df[df["bank_id"].isin(bank_ids)]
    n = 0
    with out.open("w", encoding="utf-8") as f:
        for _, r in rows.iterrows():
            if not r["source"].startswith(("swebench_", "swelive_", "swerebench_", "multiswebench")):
                continue
            patch = unseal(r["bank_id"])
            f.write(json.dumps({"instance_id": r["source_id"], "model_patch": patch,
                                "model_name_or_path": "gold_audit"}, ensure_ascii=False) + "\n")
            n += 1
    return n


def run_swebench_gold(pred_file: Path, run_id: str, dataset_hint: str) -> bool:
    """调官方 harness 验证 gold。dataset 需与题目来源一致（ Verified 用原数据集名即可）。"""
    repo = Path(__file__).parent.parent / "sources" / "repos" / "swebench"
    cmd = [
        "uv", "run", "--python", "3.11", "--with", "swebench",
        "python", "-m", "swebench.harness.run_evaluation",
        "--predictions_path", str(pred_file),
        "--dataset_name", dataset_hint,
        "--run_id", run_id,
        "--max_workers", "2",
    ]
    print("  ", " ".join(cmd[:2]), "...", dataset_hint)
    cp = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=7200)
    ok = cp.returncode == 0
    tail = (cp.stdout + cp.stderr).strip()[-500:]
    print(tail)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default=None)
    ap.add_argument("--bank-ids", default=None)
    ap.add_argument("--dataset", default="princeton-nlp/SWE-bench_Verified",
                    help="swebench harness 用的数据集名（需与题目同源）")
    args = ap.parse_args()

    if args.bank_ids:
        ids = args.bank_ids.split(",")
    elif args.report:
        ids = [r["bank_id"] for r in json.loads(Path(args.report).read_text(encoding="utf-8"))]
    else:
        print("需要 --bank-ids 或 --report")
        return 1

    import pandas as pd
    from common import load_index
    from env import LOGS

    df = load_index()
    rows = df[df["bank_id"].isin(ids)]
    plan = {"swebench_gold": [], "oj_skip": [], "audit_pending": []}
    for _, r in rows.iterrows():
        if r["verifier_type"] == "f2p_p2p_tests" and r["source"].startswith(
                ("swebench_", "swelive_", "swerebench_", "multiswebench")):
            plan["swebench_gold"].append(r["bank_id"])
        elif r["verifier_type"] == "oj_tests":
            plan["oj_skip"].append(r["bank_id"])
        else:
            plan["audit_pending"].append(r["bank_id"])

    print(f"swebench 金标检: {len(plan['swebench_gold'])} | OJ 跳过: {len(plan['oj_skip'])} | 待适配: {len(plan['audit_pending'])}")

    results = {}
    if plan["swebench_gold"]:
        pred = LOGS / "gold_audit_preds.jsonl"
        n = gold_predictions(plan["swebench_gold"], pred)
        print(f"解封 {n} 条金标 → {pred}（检完即删）")
        ok = run_swebench_gold(pred, f"gold-{time.strftime('%H%M%S')}", args.dataset)
        results["swebench_gold_harness_ok"] = ok
        pred.unlink(missing_ok=True)   # 金标临时文件立即删除
    (LOGS / "last_audit.json").write_text(
        json.dumps({"plan": {k: v for k, v in plan.items()}, "results": results},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    print("结果: logs/last_audit.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
