"""M1 采样：py_fix 域 BIP 父题分层抽样 → 注入候选（fixed 基线 × 历史金标源文件 × 测试上下文）。

只选 fixed 快照已缓存的父题（离线安全）；目标文件 = 父题金标涉及源文件
（历史 bug 位点 = 注入沃土），50~400 行；测试文件取自 bug.info。

用法: D:/tikutaku/venv/Scripts/python.exe tools/smith_sample.py [--n 100] [--max-per-repo 12] [--seed 20261026]
输出: logs/smith/candidates.jsonl
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smith_common as sc  # noqa: E402


def read_test_files(bug_dir: Path, snapshot: Path) -> list[str]:
    """bug.info 的 test_file="a;b" → 存在于 fixed 快照的相对路径列表。"""
    info_path = bug_dir / "bug.info"
    if not info_path.exists():
        return []
    raw = info_path.read_bytes()
    info = raw.decode("utf-16") if raw[:2] == b"\xff\xfe" else raw.decode("utf-8", errors="replace")
    m = re.search(r'test_file="([^"]+)"', info)
    if not m:
        return []
    out = []
    for tf in m.group(1).split(";"):
        tf = tf.strip()
        if tf and (snapshot / tf).exists():
            out.append(tf)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="SWE-smith 注入候选采样")
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--max-per-repo", type=int, default=12)
    ap.add_argument("--seed", type=int, default=20261026)
    args = ap.parse_args()

    import pandas as pd
    import scoring
    from bip_gold_audit import FIXED_ROOT
    from env import INDEX

    df = pd.read_parquet(INDEX)
    pool = df[(df.source == "bugsinpy") & (df.verifier_type == "bip_tests")
              & df.fail_to_pass.notna() & (df.fail_to_pass != "[]") & (~df.retired.astype(bool))]
    print(f"BIP 可用父题池: {len(pool)}")

    rng = random.Random(args.seed)
    parents = list(pool.itertuples())
    rng.shuffle(parents)

    per_repo: dict[str, int] = {}
    rows: list[dict] = []
    skipped = {"no_env": 0, "no_snapshot": 0, "no_test_files": 0, "no_target_file": 0, "repo_cap": 0}
    for r in parents:
        if len(rows) >= args.n:
            break
        repo = str(r.repo or "")
        try:
            env = json.loads(r.env_notes or "{}")
        except Exception:
            env = {}
        if not (env.get("repo_url") and env.get("fixed_commit") and env.get("bug_dir")):
            skipped["no_env"] += 1
            continue
        if per_repo.get(repo, 0) >= args.max_per_repo:
            skipped["repo_cap"] += 1
            continue
        snapshot = FIXED_ROOT / r.bank_id
        if not (snapshot.exists() and any(snapshot.iterdir())):
            skipped["no_snapshot"] += 1
            continue
        tests = read_test_files(Path(env["bug_dir"]), snapshot)
        if not tests:
            skipped["no_test_files"] += 1
            continue
        # 目标文件：父题金标源文件（剔测试），行数 50~400，按序取第一个合格的
        target = None
        for p in scoring.d4j_gold_paths(r.bank_id):
            if p in tests or not p.endswith(".py"):
                continue
            fp = snapshot / p
            if not fp.exists():
                continue
            n_lines = len(fp.read_text(encoding="latin-1").splitlines())
            if 50 <= n_lines <= 800:
                target = (p, n_lines)
                break
        if not target:
            skipped["no_target_file"] += 1
            continue
        per_repo[repo] = per_repo.get(repo, 0) + 1
        rows.append({
            "cand_id": f"C{len(rows) + 1:03d}",
            "parent_bid": r.bank_id,
            "parent_source_id": r.source_id,
            "repo": repo,
            "repo_url": env["repo_url"],
            "base_commit": env["fixed_commit"],
            "bug_dir": env["bug_dir"],
            "test_files": tests,
            "target_path": target[0],
            "target_lines": target[1],
        })

    sc.CANDIDATES.parent.mkdir(parents=True, exist_ok=True)
    sc.CANDIDATES.write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in rows) + "\n", encoding="utf-8")
    print(f"候选 {len(rows)} 个 → {sc.CANDIDATES}")
    print(f"项目分布: {json.dumps(per_repo, ensure_ascii=False)}")
    print(f"跳过: {json.dumps(skipped)}")
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
