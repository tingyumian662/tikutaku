"""M4 审计：金标还原复跑（干净快照上 F2P 节点必须全过）+ glm-5.3-flash 抽检合理性。

- 金标还原复跑 = bip_gold_audit 的反向应用：合成题"还原"= 干净基线文件，
  F2P 节点在干净快照上必须全过（同时兜住 flaky 假阳性）。
- flash QC 看的是 diff + 题面：bug 是否"合理但错误"、题面是否泄答案。
  严格 JSON 判定，解析失败按拒收处理（保守侧）。

用法: D:/tikutaku/venv/Scripts/python.exe tools/smith_audit.py [--qc-sample 10] [--timeout 600]
输入: logs/smith/verified.jsonl → 输出: logs/smith/audited.jsonl
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smith_common as sc  # noqa: E402

sys.path.insert(0, str(sc.ROOT / "harness"))
sys.path.insert(0, str(sc.ROOT / "tools"))
from adapters import api_agent  # noqa: E402
from smith_verify import run_pytest  # noqa: E402

# M3.5 重写后的去泄漏题面（cand_id → statement），审计与入库以它为准
RESTATED: dict[str, str] = {}
_restated_path = sc.SMITH_DIR / "restated.jsonl"
if _restated_path.exists():
    RESTATED = {x["cand_id"]: x["statement"] for x in sc.read_jsonl(_restated_path)
                if x.get("status") == "ok"}

QC_SYSTEM = (
    "You review synthetic bug-fix tasks for a private code benchmark. You get the diff "
    "that introduces the bug, the problem statement shown to test-taker models, and the "
    "tests that must fail. Judge strictly, answer with ONE JSON object only:\n"
    '{"plausible": true|false, "leak": true|false, "comment": "<=30 words"}\n'
    "plausible = the bug is subtle, realistic, and fixable without guessing intent "
    "(not gross breakage, not syntax games, not test-gaming).\n"
    "leak = the statement (directly or nearly) reveals the fix or the faulty line."
)


def venv_of(parent_bid: str) -> str | None:
    cache = json.loads(sc.BASELINE_CACHE.read_text(encoding="utf-8")) if sc.BASELINE_CACHE.exists() else {}
    return (cache.get(parent_bid) or {}).get("venv")


def gold_recheck(r: dict, timeout: float) -> str:
    """干净快照（金标还原态）上 F2P 节点全过。"""
    snap = sc.fixed_snapshot(r["parent_bid"], r["repo_url"], r["base_commit"])
    venv = venv_of(r["parent_bid"])
    if snap is None or not venv:
        return "fail:no_env"
    res = run_pytest(snap, venv, r["f2p"], timeout)
    if "timeout_or_error" in res:
        return f"fail:{res['timeout_or_error']}"
    if res["failed"] or res["errored"]:
        return f"fail:{len(res['failed'])}F/{len(res['errored'])}E on clean"
    return "pass"


def qc_review(r: dict, ledger: sc.UsageLedger) -> dict | None:
    """flash 质检：diff + 题面 → {plausible, leak, comment}；解析失败返回 None（按拒收）。"""
    snap = sc.fixed_snapshot(r["parent_bid"], r["repo_url"], r["base_commit"])
    clean = (snap / r["target_path"]).read_text(encoding="latin-1")
    diff = "\n".join(difflib.unified_diff(clean.splitlines(), r["buggy_content"].splitlines(),
                                          fromfile=r["target_path"], tofile=r["target_path"],
                                          lineterm="", n=3))
    user = (f"## Bug-introducing diff\n```diff\n{diff}\n```\n\n"
            f"## Problem statement shown to models\n"
            f"{RESTATED.get(r['cand_id'], r['statement'])}\n\n"
            f"## Tests that must fail after the bug\n" + "\n".join(f"- {t}" for t in r["f2p"]))
    try:
        text, usage = api_agent.chat(
            [{"role": "system", "content": QC_SYSTEM}, {"role": "user", "content": user}],
            model=sc.QC_MODEL, base_url=None, api_key=None, protocol=None,
            max_tokens=8192, temperature=0.0, timeout=300, retries=3, thinking_level="medium")
    except Exception as e:  # noqa: BLE001
        return {"plausible": False, "leak": True, "comment": f"qc_api_error:{type(e).__name__}"}
    finally:
        ledger.add(sc.QC_MODEL, usage)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {"plausible": False, "leak": True, "comment": "qc_unparsed"}
    try:
        v = json.loads(m.group(0))
        return {"plausible": bool(v.get("plausible")), "leak": bool(v.get("leak")),
                "comment": str(v.get("comment", ""))[:200]}
    except Exception:  # noqa: BLE001
        return {"plausible": False, "leak": True, "comment": "qc_bad_json"}


def main() -> int:
    ap = argparse.ArgumentParser(description="金标还原复跑 + flash 抽检")
    ap.add_argument("--qc-sample", type=int, default=10)
    ap.add_argument("--timeout", type=float, default=600)
    args = ap.parse_args()

    verified = [r for r in sc.read_jsonl(sc.VERIFIED) if r.get("status") == "ok"]
    done = {r["cand_id"] for r in sc.read_jsonl(sc.AUDITED)}
    todo = [r for r in verified if r["cand_id"] not in done]
    print(f"鉴别通过 {len(verified)} | 已审计 {len(done)} | 本轮 {len(todo)}（QC 抽检 {args.qc_sample}）")
    ledger = sc.UsageLedger()
    qc_budget = args.qc_sample
    stats: dict[str, int] = {}
    for r in todo:
        t0 = time.time()
        row = {k: r[k] for k in ("cand_id", "parent_bid", "repo", "repo_url", "base_commit",
                                 "bug_dir", "test_files", "target_path",
                                 "buggy_content", "model", "gen_at", "f2p", "n_failed", "n_total")}
        row["statement"] = RESTATED.get(r["cand_id"], r["statement"])
        row["gold_recheck"] = gold_recheck(r, args.timeout)
        row["qc"] = qc_review(r, ledger) if qc_budget > 0 else None
        if row["qc"] is not None:
            qc_budget -= 1
        ok = row["gold_recheck"] == "pass" and (row["qc"] is None or
                                                (row["qc"]["plausible"] and not row["qc"]["leak"]))
        row["status"] = "ok" if ok else "reject"
        stats[row["status"]] = stats.get(row["status"], 0) + 1
        sc.append_jsonl(sc.AUDITED, row)
        qc_note = "" if row["qc"] is None else f" qc={row['qc']['plausible']}/{not row['qc']['leak']}"
        print(f"  {row['cand_id']} {row['repo']:<12} {row['status']:<6} {row['gold_recheck'][:44]}"
              f"{qc_note} {round(time.time() - t0, 1)}s", flush=True)
    print(f"\n本轮: {stats} | 记账: {ledger.summary()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
