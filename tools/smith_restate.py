"""M3.5 题面重写：满血看 diff 把题面改写成纯外部症状描述（去机制泄漏）。

背景：注入器的题面常把 bug 机制写出来（"insensitive to true labels"式点名），
QC 判 leak 属实。重写规则：只描述用户/测试可观察的错误行为，禁止提及 diff 中
出现的任何代码标识符（函数名除外）、禁止"instead of/rather than/should be"式
正确语义表述。

用法: D:/tikutaku/venv/Scripts/python.exe tools/smith_restate.py [--limit N] [--concurrency 2]
输入: logs/smith/verified.jsonl（status=ok）→ 输出: logs/smith/restated.jsonl
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smith_common as sc  # noqa: E402

sys.path.insert(0, str(sc.ROOT / "harness"))
sys.path.insert(0, str(sc.ROOT / "tools"))
from adapters import api_agent  # noqa: E402

RESTATED = sc.SMITH_DIR / "restated.jsonl"

SYSTEM = (
    "You rewrite bug reports for a private code benchmark. You see a bug-introducing diff "
    "and a draft bug report. Rewrite the report so it describes ONLY externally observable "
    "behavior: what goes wrong for a user or caller (wrong values, wrong routing, wrong "
    "output), reproducible at a high level. 2-4 sentences, English, maintainer bug-report tone.\n"
    "Hard rules:\n"
    "- You may name the affected public function/class, but NO other code identifiers\n"
    "  (no variable, argument, constant, or module names from the diff).\n"
    "- Do NOT state or hint the correct semantics, the fix, or the mechanism\n"
    "  (no 'instead of X', 'should use Y', 'ignores Z', 'treats A as B').\n"
    "- Do NOT include repro code or exact expected/actual value pairs that pin the faulty line.\n"
    "Reply with the rewritten report only, nothing else."
)


def process(r: dict, ledger: sc.UsageLedger) -> dict:
    snap = sc.fixed_snapshot(r["parent_bid"], r["repo_url"], r["base_commit"])
    clean = (snap / r["target_path"]).read_text(encoding="latin-1")
    diff = "\n".join(difflib.unified_diff(clean.splitlines(), r["buggy_content"].splitlines(),
                                          fromfile=r["target_path"], tofile=r["target_path"],
                                          lineterm="", n=2))
    user = (f"## Bug-introducing diff\n```diff\n{diff}\n```\n\n"
            f"## Draft bug report to rewrite\n{r['statement']}")
    t0 = time.time()
    try:
        text, usage = api_agent.chat(
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
            model=sc.GENERATOR, base_url=None, api_key=None, protocol=None,
            max_tokens=8192, temperature=0.3, timeout=600, retries=3, thinking_level="medium")
    except Exception as e:  # noqa: BLE001
        return {"cand_id": r["cand_id"], "status": "reject", "reason": f"api_error:{type(e).__name__}"}
    finally:
        ledger.add(sc.GENERATOR, usage)
    out = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text.strip())
    if not (20 <= len(out) <= 1500):
        return {"cand_id": r["cand_id"], "status": "reject", "reason": "bad_len"}
    return {"cand_id": r["cand_id"], "status": "ok", "statement": out,
            "sec": round(time.time() - t0, 1)}


def main() -> int:
    ap = argparse.ArgumentParser(description="题面去泄漏重写（可续跑）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=2)
    args = ap.parse_args()

    verified = [r for r in sc.read_jsonl(sc.VERIFIED) if r.get("status") == "ok"]
    done = {x["cand_id"] for x in sc.read_jsonl(RESTATED)}
    todo = [r for r in verified if r["cand_id"] not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f"鉴别通过 {len(verified)} | 已重写 {len(done)} | 本轮 {len(todo)}")
    ledger = sc.UsageLedger()
    stats: dict[str, int] = {}

    def run_one(r):
        row = process(r, ledger)
        stats[row["status"]] = stats.get(row["status"], 0) + 1
        sc.append_jsonl(RESTATED, row)
        print(f"  {r['cand_id']} {r['repo']:<12} {row['status']:<6} {row.get('reason', '')[:40]} "
              f"{row.get('sec', '?')}s", flush=True)

    if todo:
        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            list(ex.map(run_one, todo))
    print(f"\n本轮: {stats} | 记账: {ledger.summary()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
