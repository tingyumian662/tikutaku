"""M2 注入：glm-5.3 满血对候选生成"合理但错误"的 SEARCH/REPLACE 变体 + 症状式题面。

输出走仓库标准 S/R 块（apply_search_replace 同款），避免整文件重写的截断/省略占位问题。
硬校验（smith_common.check_injection）：语法合法 / 顶层签名+装饰器+类基不变 /
改动 ≤10 行 / 确有改动。拒绝原因落 injected.jsonl（status=reject 行）。
可续跑：已处理的 cand_id 自动跳过。GLM 并发 ≤2（用户在线红线）。

用法: D:/tikutaku/venv/Scripts/python.exe tools/smith_inject.py [--limit N] [--concurrency 2]
输出: logs/smith/injected.jsonl + usage.json 记账
"""
from __future__ import annotations

import argparse
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

TEST_CTX_MAX_LINES = 600
THINKING = "high"
TEMPERATURE = 0.3

SYSTEM = (
    "You are a subtle bug injector replicating the SWE-smith methodology for a private "
    "code-benchmark pipeline. You receive one source file of a real open-source project "
    "and its test file. Introduce exactly ONE subtle, plausible bug via SEARCH/REPLACE "
    "blocks, then describe the symptom. Reply in exactly this format, nothing else:\n"
    "### file: <the target file path>\n"
    "<<<<<<< SEARCH\n"
    "<verbatim snippet from the source file, with enough context to be unique>\n"
    "=======\n"
    "<the same snippet with your single subtle defect introduced>\n"
    ">>>>>>> REPLACE\n"
    "(one block is usually enough; keep total changed lines <= 10)\n"
    "===STATEMENT===\n"
    "<2-5 sentence English bug report: observed symptom, actual vs expected behavior, "
    "written as a maintainer's report. Do NOT reveal the fix, the faulty line, or the mechanism.>\n"
    "===REASON===\n"
    "<one sentence: what you changed and which existing test should catch it>"
)

RULES = """Bug requirements:
- ONE defect only, in the target file: wrong operator/boundary/condition, off-by-one,
  swapped or wrong variable, incorrect default, mishandled edge case, inverted
  condition, dropped clause.
- It must look like an honest mistake a competent developer could have made.
- Do NOT change any function/method signature, decorator, class definition, or import
  of the file's public surface.
- The file must stay syntactically valid and importable.
- At least one test in the referenced test file must fail because of the bug; most
  tests should still pass (subtle, not gross breakage).
- No randomness, no time/network dependence, no touching test files, no debug prints."""


def build_user(cand: dict, file_content: str, test_ctx: str) -> str:
    return (f"Project: {cand['repo']} (open-source Python project)\n"
            f"Target file: {cand['target_path']}\n\n"
            f"### file: {cand['target_path']}\n```\n{file_content}\n```\n\n"
            f"## Existing tests (must keep mostly passing; at least one must fail)\n{test_ctx}\n\n"
            f"{RULES}")


def _marker(text: str, key: str) -> str | None:
    m = re.search(rf"==={key}===\s*(.*?)(?====\w===|\Z)", text, re.S)
    return m.group(1).strip() if m else None


def clean_file_from_snapshot(cand: dict) -> str | None:
    fp = sc.fixed_snapshot(cand["parent_bid"], cand["repo_url"], cand["base_commit"]) / cand["target_path"]
    return fp.read_text(encoding="latin-1") if fp.exists() else None


def test_context(cand: dict) -> str:
    snap = sc.fixed_snapshot(cand["parent_bid"], cand["repo_url"], cand["base_commit"])
    chunks = []
    for tf in cand["test_files"]:
        fp = snap / tf
        if not fp.exists():
            continue
        lines = fp.read_text(encoding="latin-1").splitlines()
        body = "\n".join(lines[:TEST_CTX_MAX_LINES])
        note = f"\n... ({len(lines) - TEST_CTX_MAX_LINES} more lines truncated)" if len(lines) > TEST_CTX_MAX_LINES else ""
        chunks.append(f"--- {tf} ---\n{body}{note}")
    return "\n\n".join(chunks)


def process(cand: dict, ledger: sc.UsageLedger) -> dict:
    clean = clean_file_from_snapshot(cand)
    if clean is None:
        return {**cand, "status": "reject", "reason": "clean_file_missing"}
    user = build_user(cand, clean, test_context(cand))
    t0 = time.time()
    try:
        # max_tokens 含 thinking：high 档预算 16k + 文本余量 16k
        text, usage = api_agent.chat(
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
            model=sc.GENERATOR, base_url=None, api_key=None, protocol=None,
            max_tokens=32768, temperature=TEMPERATURE, timeout=900, retries=3,
            thinking_level=THINKING)
    except Exception as e:  # noqa: BLE001
        return {**cand, "status": "reject", "reason": f"api_error:{type(e).__name__}:{str(e)[:120]}"}
    finally:
        ledger.add(sc.GENERATOR, usage)
    statement = _marker(text, "STATEMENT")
    reason = _marker(text, "REASON")
    if not statement or not reason:
        return {**cand, "status": "reject", "reason": "format_unparsed",
                "raw_head": text[:200], "sec": round(time.time() - t0, 1)}
    patch_files, _, sr_errors = api_agent.apply_search_replace([(cand["target_path"], clean)], text)
    if sr_errors:
        return {**cand, "status": "reject", "reason": f"sr:{sr_errors[0]}",
                "sec": round(time.time() - t0, 1)}
    if cand["target_path"] not in patch_files:
        return {**cand, "status": "reject", "reason": "no_patch_block",
                "sec": round(time.time() - t0, 1)}
    new_file = patch_files[cand["target_path"]]
    err = sc.check_injection(cand["target_path"], clean, new_file)
    if err:
        return {**cand, "status": "reject", "reason": f"check:{err}", "sec": round(time.time() - t0, 1)}
    if not (20 <= len(statement) <= 1500):
        return {**cand, "status": "reject", "reason": "bad_statement_len", "sec": round(time.time() - t0, 1)}
    return {**cand, "status": "ok", "statement": statement, "reason_internal": reason,
            "buggy_content": new_file, "model": sc.GENERATOR,
            "gen_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "sec": round(time.time() - t0, 1)}


def main() -> int:
    ap = argparse.ArgumentParser(description="glm-5.3 缺陷注入（可续跑）")
    ap.add_argument("--limit", type=int, default=0, help="本轮最多处理数（0=全部待办）")
    ap.add_argument("--concurrency", type=int, default=2)
    args = ap.parse_args()

    cands = sc.read_jsonl(sc.CANDIDATES)
    done = {r["cand_id"] for r in sc.read_jsonl(sc.INJECTED)}
    todo = [c for c in cands if c["cand_id"] not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f"候选 {len(cands)} | 已处理 {len(done)} | 本轮 {len(todo)}（并发 {args.concurrency}）")
    if not todo:
        return 0

    ledger = sc.UsageLedger()
    stats: dict[str, int] = {}

    def run_one(c):
        row = process(c, ledger)
        stats[row["status"]] = stats.get(row["status"], 0) + 1
        sc.append_jsonl(sc.INJECTED, row)
        print(f"  {c['cand_id']} {c['repo']:<12} {row['status']:<6} {row.get('reason', '')[:60]} "
              f"{row.get('sec', '?')}s", flush=True)
        return row

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        list(ex.map(run_one, todo))
    print(f"\n本轮: {stats} | 累计记账: {ledger.summary()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
