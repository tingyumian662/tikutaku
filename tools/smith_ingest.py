"""M5 入库：审计通过的合成题 → seal 金标 + 任务目录 + 物化工作区 + 增量写索引。

- 金标 = unified diff（带 diff --git 头），d4j_gold_paths 零改动可提取路径；
  seal 后自检路径解析正确才入库。
- 任务目录写两个文件：origin.json（元数据，无金标内容）、synth_bug.json（注入后
  文件全文，run.py 钩子物化用）。
- 运行时工作区预物化：复制父题 fixed 快照 → D:/tikutaku/bip_tasks/<SYN-id> 再打上
  注入文件，bip_checkout 幂等缓存直接命中（离线安全）。
- 索引增量 concat + cutoff_filter.judge 打防污染标（synthetic 全模型 ok）。
  绝不走 build_index（全量重建丢 tier_rank/level）。
- 隔离：source=swesmith_local 不在 runner_core.DOMAINS，天然不入池/锚卷/榜单。

用法: D:/tikutaku/venv/Scripts/python.exe tools/smith_ingest.py [--dry-run]
输入: logs/smith/audited.jsonl（status=ok）→ 索引 + bank/tasks/SYN-*
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smith_common as sc  # noqa: E402

sys.path.insert(0, str(sc.ROOT / "harness"))
sys.path.insert(0, str(sc.ROOT / "tools"))
import scoring  # noqa: E402
from cutoff_filter import judge  # noqa: E402
from seal_gold import seal  # noqa: E402

INGESTED = sc.SMITH_DIR / "ingested.jsonl"


def ingest_one(r: dict, bank_id: str, dry: bool) -> dict:
    snap = sc.fixed_snapshot(r["parent_bid"], r["repo_url"], r["base_commit"])
    clean = (snap / r["target_path"]).read_text(encoding="latin-1")
    # 双保险：入库前重跑注入硬校验（buggy_content 与干净文件再比一次）
    err = sc.check_injection(r["target_path"], clean, r["buggy_content"])
    if err:
        return {"cand_id": r["cand_id"], "status": "reject", "reason": f"recheck:{err}"}
    task_dir = sc.ROOT / "bank" / "tasks" / bank_id
    env_notes = {
        "repo_url": r["repo_url"], "buggy_commit": r["base_commit"], "fixed_commit": r["base_commit"],
        "bug_dir": r["bug_dir"], "parent": r["parent_bid"], "synthetic": True,
        "generator": sc.GENERATOR, "qc": sc.QC_MODEL,
    }
    origin = {
        "synthetic": True, "pipeline": "smith-pilot-1", "parent": r["parent_bid"],
        "parent_source_id": r.get("parent_source_id", ""), "target_path": r["target_path"],
        "statement": r["statement"], "f2p": r["f2p"], "n_failed": r["n_failed"],
        "n_total": r["n_total"], "generator": sc.GENERATOR, "gen_at": r["gen_at"],
        "test_files": r["test_files"],
    }
    if dry:
        return {"cand_id": r["cand_id"], "status": "dry", "bank_id": bank_id}
    # 1) seal 金标（unified diff）+ 自检 d4j_gold_paths
    seal(bank_id, sc.seal_diff(r["target_path"], r["buggy_content"], clean))
    paths = scoring.d4j_gold_paths(bank_id)
    if paths != [r["target_path"]]:
        return {"cand_id": r["cand_id"], "status": "reject",
                "reason": f"gold_paths_mismatch:{paths}"}
    # 2) 任务目录（origin 无金标内容；synth_bug = 注入后文件，运行时物化用）
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "origin.json").write_text(json.dumps(origin, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    (task_dir / "synth_bug.json").write_text(
        json.dumps({r["target_path"]: r["buggy_content"]}, ensure_ascii=False), encoding="utf-8")
    # 3) 预物化运行时工作区：复制父题干净快照 → 打注入文件（bip_checkout 缓存命中）
    wd = scoring.BIP_TASKS_ROOT / bank_id
    if not wd.exists():
        shutil.copytree(snap, wd, symlinks=True)
    fp = wd / r["target_path"]
    fp.write_text(r["buggy_content"], encoding="latin-1", newline="\n")
    # 4) 索引行
    from schema import new_task
    row = new_task(
        bank_id=bank_id, source=sc.SOURCE_KEY, source_id=f"{r.get('parent_source_id', '?')}-s1",
        language="python", repo=r["repo"], task_type="bug_fix",
        problem_statement=r["statement"], gold_seal=f"{bank_id}.bin",
        verifier_type="bip_tests", fail_to_pass=json.dumps(r["f2p"], ensure_ascii=False),
        pass_to_pass="[]", timeout_sec=900, env_type="venv",
        env_notes=json.dumps(env_notes, ensure_ascii=False), notes="synthetic")
    row["cutoff_ok_json"] = json.dumps(judge(row), ensure_ascii=False)
    return {"cand_id": r["cand_id"], "status": "ok", "bank_id": bank_id, "index_row": row}


def main() -> int:
    ap = argparse.ArgumentParser(description="合成题增量入库（幂等可续跑）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    audited = [r for r in sc.read_jsonl(sc.AUDITED) if r.get("status") == "ok"]
    done = {x["cand_id"] for x in sc.read_jsonl(INGESTED) if x.get("status") == "ok"}
    todo = [r for r in audited if r["cand_id"] not in done]
    print(f"审计通过 {len(audited)} | 已入库 {len(done)} | 本轮 {len(todo)}"
          f"{'（dry-run）' if args.dry_run else ''}")
    if not todo:
        return 0

    import pandas as pd
    from env import INDEX
    from importers.common import save_index

    seq = sc.next_seq()
    new_rows, results = [], []
    for r in todo:
        bank_id = f"{sc.PREFIX}-{seq:06d}"
        res = ingest_one(r, bank_id, args.dry_run)
        sc.append_jsonl(INGESTED, res)
        results.append(res)
        seq += 1  # dry-run 也前进，预览序号与真实入库一致
        if res["status"] == "ok":
            new_rows.append(res.pop("index_row"))
        print(f"  {r['cand_id']} → {res.get('bank_id', '?'):<12} {res['status']:<6} "
              f"{res.get('reason', '')}", flush=True)
    if new_rows and not args.dry_run:
        df = pd.read_parquet(INDEX)
        assert not df.bank_id.isin([x["bank_id"] for x in new_rows]).any(), "bank_id 撞号"
        df = pd.concat([df, pd.DataFrame(new_rows)], ignore_index=True)
        df["retired"] = df["retired"].astype(bool)
        df["timeout_sec"] = df["timeout_sec"].astype("int64")
        save_index(df)
        print(f"\n入库 {len(new_rows)} 题（{new_rows[0]['bank_id']}..{new_rows[-1]['bank_id']}）")
    ok = sum(1 for x in results if x["status"] in ("ok", "dry"))
    print(f"结果: ok {ok} / reject {len(results) - ok}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
