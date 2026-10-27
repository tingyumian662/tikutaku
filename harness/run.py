"""主运行器：选题 → 起 agent（API/CLI 双适配器）→ 判分 → 出成绩单。

用法示例:
  # LiveCodeBench 100 题，API 评测
  uv run python harness/run.py --view lcb_easy --n 100 --adapter api --model glm-4.6 \
      --base-url https://open.bigmodel.cn/api/paas/v4

  # 已物化的 SWE 题，CLI 评测（claude/codex 自动探测）
  uv run python harness/run.py --report logs/last_pull_report.json --adapter cli

输出: results/<run_id>/results.jsonl + leaderboard.md
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "harness"))

from adapters import api_agent, cli_agent  # noqa: E402
import scoring  # noqa: E402
import perf_scoring  # noqa: E402
from seal_gold import unseal  # noqa: E402


def load_selection(args) -> list[dict]:
    import json as _json

    import pandas as pd
    sys.path.insert(0, str(ROOT / "tools" / "importers"))
    from common import load_index

    df = load_index()
    if args.report:
        ids = [r["bank_id"] for r in _json.loads(Path(args.report).read_text(encoding="utf-8"))]
    elif args.view:
        ids = _json.loads((ROOT / "bank" / "views" / f"{args.view}.json").read_text(encoding="utf-8"))
    elif getattr(args, "anchor", None):
        doc = _json.loads((ROOT / "bank" / "anchors" / f"{args.anchor}.json").read_text(encoding="utf-8"))
        ids = doc["bank_ids"]
    elif args.bank_ids:
        ids = args.bank_ids.split(",")
    else:
        raise SystemExit("需要 --view / --report / --bank-ids / --anchor 之一")
    if args.n:
        ids = ids[: args.n]
    if args.skip_done:
        from env import RESULTS
        done = set()
        for f in RESULTS.glob("run-*/results.jsonl"):
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    r = _json.loads(line)
                except Exception:
                    continue
                # 只跳过"已出有效判定"的题；error 的（限流等）重试
                if r.get("model") == args.model and \
                        r.get("score", {}).get("verdict") in ("pass", "wrong_answer", "timeout", "compile_error", "apply_fail"):
                    done.add(r.get("bank_id"))
        before = len(ids)
        ids = [i for i in ids if i not in done]
        print(f"断点续跑: 跳过已完成 {before - len(ids)} 题")
    rows = df[df["bank_id"].isin(ids)]
    print(f"评测 {len(rows)} 题 | adapter={args.adapter} model={args.model or 'CLI'} | 并发={args.concurrency}")
    return rows.to_dict("records")


def run_one(task: dict, args) -> dict:
    from env import TASKS

    t0 = time.time()
    rec = {"bank_id": task["bank_id"], "source": task["source"], "source_id": task["source_id"],
           "task_type": task["task_type"], "model": args.model or "cli", "started": time.strftime("%H:%M:%S")}

    if args.adapter == "api":
        problem = task["problem_statement"] or ""
        origin = json.loads((TASKS / task["bank_id"] / "origin.json").read_text(encoding="utf-8")) \
            if (TASKS / task["bank_id"] / "origin.json").exists() else {}
        protocol = getattr(args, "protocol", None)
        if protocol in (None, "auto"):
            protocol = None
        kw = dict(
            max_tokens=int(getattr(args, "max_tokens", 0) or 8192),
            temperature=float(getattr(args, "temperature", 0.2)),
            timeout=float(getattr(args, "task_timeout", 0) or 600),
            system_extra=str(getattr(args, "system_extra", "") or ""),
            thinking_level=getattr(args, "thinking_level", "max"),
        )
        if task["verifier_type"] == "oj_tests":
            out = api_agent.solve_algo(problem, str(origin.get("start_code", "")),
                                       args.model, args.base_url, args.api_key, protocol=protocol, **kw)
            rec.update(out)
            rec["score"] = scoring.score_oj(out["answer"], json.loads(task["fail_to_pass"]),
                                            timeout_sec=int(getattr(args, "score_timeout", 0) or 20))
        elif task["verifier_type"] == "bip_tests":
            # BugsInPy：codeload 物化 → oracle 文件上下文 → SEARCH/REPLACE → WSL pytest 判分
            f2p = json.loads(task["fail_to_pass"] or "[]")
            try:
                env = json.loads(task.get("env_notes") or "{}")
            except Exception:
                env = {}
            workdir = scoring.bip_checkout(task["bank_id"], env.get("repo_url", ""),
                                           env.get("buggy_commit", ""))
            if workdir is None:
                rec.update({"answer": "", "usage": {}})
                rec["score"] = {"verdict": "error:checkout_fail"}
            else:
                # 合成题（synth_bug.json 存在）：基线即 fixed 提交，注入文件覆盖后测试已在位，
                # 跳过 fixed 测试覆盖（否则按新 bank_id 重复拉同款 tarball）
                synth_bug = TASKS / task["bank_id"] / "synth_bug.json"
                if synth_bug.exists():
                    for p, c in json.loads(synth_bug.read_text(encoding="utf-8")).items():
                        sfp = workdir / p
                        if sfp.exists():
                            sfp.write_text(c, encoding="latin-1", newline="\n")
                else:
                    # F2P 测试方法只在 fixed 版存在：用 fixed 的测试文件覆盖工作区（BIP 官方流程）
                    scoring.bip_prepare_test(workdir, task["bank_id"], env.get("repo_url", ""),
                                             env.get("fixed_commit", ""),
                                             Path(env["bug_dir"]) if env.get("bug_dir") else None)
                files = []
                for p in scoring.d4j_gold_paths(task["bank_id"]):
                    fp = workdir / p
                    if fp.exists():
                        files.append((p, fp.read_text(encoding="latin-1")))
                venv = scoring.bip_venv(
                    str(task.get("repo") or "misc"),
                    Path(env["bug_dir"]) / "requirements.txt" if env.get("bug_dir") else None,
                    bank_id=str(env.get("parent") or task["bank_id"]))  # 合成题沿用父题 venv 路由
                if venv is None:
                    rec.update({"answer": "", "usage": {}})
                    rec["score"] = {"verdict": "error:venv_fail"}
                else:
                    out = api_agent.solve_patch(problem, files, f2p, args.model, args.base_url,
                                                args.api_key, protocol=protocol, **kw)
                    rec.update({"answer": "", "patch_files": list(out["patch_files"]),
                                "usage": out["usage"]})
                    if not out["raw"].strip():
                        rec["score"] = {"verdict": "error:empty_reply"}
                    else:
                        sc = scoring.score_bip(workdir, out["patch_files"], f2p, venv,
                                               timeout_sec=int(task.get("timeout_sec") or 900))
                        if out["sr_errors"]:
                            sc["sr_errors"] = out["sr_errors"][:10]
                        rec["score"] = sc
                        # BIP workdir 无 git 不能自愈：判分后恢复 buggy 原版，防下次重测读到脏文件
                        for p, orig in files:
                            fp = workdir / p
                            if fp.exists():
                                fp.write_text(orig, encoding="latin-1", newline="\n")
        elif task["verifier_type"] == "d4j_tests":
            # Defects4J 本地链路：按需 checkout → oracle 文件上下文 → SEARCH/REPLACE 修码 → 本地判分
            f2p = json.loads(task["fail_to_pass"] or "[]")
            pid, _, bug = task["source_id"].rpartition("-")
            workdir = scoring.d4j_checkout(pid, bug, task["bank_id"])
            if workdir is None:
                rec.update({"answer": "", "usage": {}})
                rec["score"] = {"verdict": "error:checkout_fail"}
            else:
                files = []
                for p in scoring.d4j_gold_paths(task["bank_id"]):
                    fp = workdir / p
                    if fp.exists():
                        # latin-1 单字节无损往返：任何字节序列读入写回不变，避免非 ASCII 损坏编译
                        files.append((p, fp.read_text(encoding="latin-1")))
                out = api_agent.solve_patch(problem, files, f2p, args.model, args.base_url,
                                            args.api_key, protocol=protocol, **kw)
                rec.update({"answer": "", "patch_files": list(out["patch_files"]),
                            "usage": out["usage"]})
                if not out["raw"].strip():
                    rec["score"] = {"verdict": "error:empty_reply", "note": "thinking 耗尽 max_tokens 或空回复"}
                else:
                    sc = scoring.score_d4j(workdir, out["patch_files"], f2p,
                                           timeout_sec=int(task.get("timeout_sec") or 1800))
                    if out["sr_errors"]:
                        sc["sr_errors"] = out["sr_errors"][:10]
                    rec["score"] = sc
        elif task["verifier_type"] == "efficiency_test":
            # SWE-Perf：物化 base → 金标涉及源文件上下文 → SEARCH/REPLACE → 效率判分
            import re as _re
            from pathlib import Path as _P
            blob = json.loads(unseal(task["bank_id"]))
            workdir = perf_scoring.perf_checkout(task["bank_id"], task["repo"], blob["base_commit"])
            if workdir is None:
                rec.update({"answer": "", "usage": {}})
                rec["score"] = {"verdict": "error:checkout_fail"}
            else:
                f2p = json.loads(task["fail_to_pass"] or "[]")
                # oracle 版提示（官方 problem_statement_oracle 语义）：指明待优化函数，收敛思考
                fns = json.loads(blob.get("patch_functions") or "{}")
                fn_hint = "；".join(f"{p}:{','.join(f)}" for p, f in fns.items())
                if fn_hint:
                    problem = (problem or "") + "\n\n待优化目标（oracle）：" + fn_hint
                perf_scoring._apply_patch(workdir, blob.get("test_patch", ""))
                files = []
                for p in _re.findall(r"diff --git a/(\S+) b/\S+", blob["patch"]):
                    if scoring._is_test_path(p):
                        continue
                    fp = workdir / p
                    if fp.exists():
                        files.append((p, fp.read_text(encoding="latin-1")))
                proj = str(task["repo"]).split("/")[-1]
                venv = perf_scoring.perf_venv(proj, str(blob.get("version", "")) or "d",
                                               install_cmd=blob.get("install_cmd", ""),
                                               spec_workdir=workdir, spec_repo=str(task["repo"]))
                if venv is None:
                    rec.update({"answer": "", "usage": {}})
                    rec["score"] = {"verdict": "error:venv_fail"}
                else:
                    out = api_agent.solve_patch(problem, files, f2p, args.model, args.base_url,
                                                args.api_key, protocol=protocol, **kw)
                    rec.update({"answer": "", "patch_files": list(out["patch_files"]),
                                "usage": out["usage"]})
                    if not out["raw"].strip():
                        rec["score"] = {"verdict": "error:empty_reply"}
                    else:
                        sc = perf_scoring.score_perf(workdir, out["patch_files"], venv,
                                                     f2p[0] if f2p else "", blob,
                                                     timeout=int(task.get("timeout_sec") or 1800))
                        if out["sr_errors"]:
                            sc["sr_errors"] = out["sr_errors"][:10]
                        rec["score"] = sc
        else:
            out = api_agent.solve_algo(problem, "", args.model, args.base_url, args.api_key, protocol=protocol, **kw)
            rec.update({"answer": "", "patch": "", "usage": out["usage"]})
            rec["score"] = {"verdict": "pending_swebench", "note": "API-swe 走 batch 模式另行判分"}
    else:
        task_dir = TASKS / task["bank_id"]
        workdir = task_dir / "workspace"
        if not (workdir / ".git").exists():
            rec["score"] = {"verdict": "no_workspace", "note": "先物化工作区（pull_tasks + prepare_workspace）"}
        else:
            problem = (task_dir / "problem.md").read_text(encoding="utf-8")
            out = cli_agent.run_cli(task_dir, workdir, problem, args.tool, task["timeout_sec"])
            rec.update(out)
            rec["score"] = {"verdict": "pending_swebench", "patch_lines": len(out.get("patch", "").splitlines())}
    rec["total_sec"] = round(time.time() - t0, 1)
    return rec


def write_leaderboard(run_dir: Path, records: list[dict]) -> None:
    solved = [r for r in records if r.get("score", {}).get("verdict") == "pass"]
    lines = [f"# 排行榜 {run_dir.name}", "",
             f"- 模型: {records[0].get('model', '?') if records else '?'}",
             f"- 题数: {len(records)}",
             f"- 通过: {len(solved)} ({len(solved) / max(len(records), 1):.1%})", "",
             "| 题号 | 源 | 判定 | 用时(s) | tokens |",
             "|---|---|---|---|---|"]
    for r in records:
        v = r.get("score", {}).get("verdict", "?")
        u = r.get("usage", {})
        tok = u.get("prompt_tokens", 0) + u.get("completion_tokens", 0)
        lines.append(f"| {r.get('bank_id', '?')} | {r.get('source', '?')} | {v} | {r.get('total_sec', '?')} | {tok} |")
    (run_dir / "leaderboard.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--view"); ap.add_argument("--report"); ap.add_argument("--bank-ids")
    ap.add_argument("--anchor", help="排位赛锚卷名（bank/anchors/<name>.json），与 --view/--bank-ids 互斥")
    ap.add_argument("-n", "--n", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=1, help="并发 API 请求数")
    ap.add_argument("--skip-done", action="store_true", help="跳过已有有效判定的题（断点续跑）")
    ap.add_argument("--adapter", choices=["api", "cli"], default="api")
    ap.add_argument("--model"); ap.add_argument("--base-url"); ap.add_argument("--api-key")
    ap.add_argument("--protocol", choices=["auto", "openai", "anthropic"], default="auto",
                    help="API 协议；auto=glm* 走 anthropic 其余走 openai")
    ap.add_argument("--max-tokens", type=int, default=131072,
                    help="每题输出 token 上限（GLM 输出上限 128K，默认拉满；思考按需消耗不浪费）")
    ap.add_argument("--temperature", type=float, default=0.2, help="温度（仅 openai 协议生效）")
    ap.add_argument("--task-timeout", type=float, default=600, help="单题 API 请求超时（秒）")
    ap.add_argument("--score-timeout", type=int, default=20, help="本地判分每个测试点超时（秒）")
    ap.add_argument("--system-extra", default="", help="追加注入的系统提示词")
    ap.add_argument("--thinking-level", choices=["max", "high", "medium", "low", "off"], default="max",
                    help="思考强度（默认 max=评测统一拉满；各家参数名自动映射，端点不认自动降级）")
    ap.add_argument("--tool", help="CLI 适配器指定工具: claude/codex/zcode")
    args = ap.parse_args()
    sel = [x for x in (args.view, args.report, args.bank_ids, args.anchor) if x]
    if len(sel) > 1:
        ap.error("--view / --report / --bank-ids / --anchor 只能给一个")

    from env import RESULTS
    run_id = time.strftime("run-%Y%m%d-%H%M%S")
    run_dir = RESULTS / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    # 配置快照（剔除密钥）：CLI run 此前不写 config.json，跨 run 对比只能翻
    # results.jsonl 的 model 字段；排位赛按 anchor_id 聚合依赖此文件
    (run_dir / "config.json").write_text(
        json.dumps({k: v for k, v in vars(args).items() if k != "api_key"},
                   ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")

    tasks = load_selection(args)
    records: list[dict] = []
    lock = __import__("threading").Lock()
    out = (run_dir / "results.jsonl").open("a", encoding="utf-8")

    def on_done(rec: dict) -> None:
        with lock:
            records.append(rec)
            out.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            out.flush()
        v = rec.get("score", {}).get("verdict", "?")
        print(f"  {rec.get('bank_id', '?'):<14} {v:<18} {rec.get('total_sec', '')}s", flush=True)

    if args.concurrency <= 1:
        for task in tasks:
            try:
                rec = run_one(task, args)
            except Exception as e:
                rec = {"bank_id": task["bank_id"], "model": args.model, "score": {"verdict": f"error:{type(e).__name__}"}, "error": str(e)[:300]}
            on_done(rec)
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futs = {pool.submit(run_one, task, args): task for task in tasks}
            for fut in as_completed(futs):
                task = futs[fut]
                try:
                    rec = fut.result()
                except Exception as e:
                    rec = {"bank_id": task["bank_id"], "model": args.model, "score": {"verdict": f"error:{type(e).__name__}"}, "error": str(e)[:300]}
                on_done(rec)
    out.close()

    write_leaderboard(run_dir, records)
    print(f"完成: {run_dir / 'leaderboard.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
