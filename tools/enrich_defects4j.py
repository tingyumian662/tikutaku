"""Defects4J 金标回填：不跑 CLI，直接从本地元数据 + git bundle 提取。

- commit-db:  bug_id, rev_buggy, rev_fixed, issue_id, issue_url
- 金标 patch = git --git-dir=project_repos/<repo>.git diff rev_buggy rev_fixed
- 触发测试 = framework/projects/<PID>/trigger_tests/<bug_id> 的 "--- 测试名" 行
- 回填 index: fail_to_pass / source_url / 重新封存金标 / 去掉 enrich_pending

用法: uv run python tools/enrich_defects4j.py   （幂等，可重跑）
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "importers"))

from env import REPOS  # noqa: E402
from seal_gold import seal  # noqa: E402

D4J = REPOS / "defects4j"
PROJ = D4J / "framework" / "projects"
BUNDLES = D4J / "project_repos"

# PID → git bundle 名（project_reobs 下实测）
PID_TO_REPO = {
    "Chart": "jfreechart", "Cli": "commons-cli", "Closure": "closure-compiler",
    "Codec": "commons-codec", "Collections": "commons-collections",
    "Compress": "commons-compress", "Csv": "commons-csv", "Dbutils": "commons-dbutils",
    "Gson": "gson", "JacksonCore": "jackson-core", "JacksonDatabind": "jackson-databind",
    "JacksonXml": "jackson-dataformat-xml", "Jsoup": "jsoup",
    "JxPath": "commons-jxpath", "Jxpath": "commons-jxpath",
    "Lang": "commons-lang", "Math": "commons-math", "Mockito": "mockito", "Time": "joda-time",
}


def sh(cmd: list[str]) -> str:
    cp = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return cp.stdout if cp.returncode == 0 else ""


def gold_diff(repo_git: Path, rev_buggy: str, rev_fixed: str) -> str:
    return sh(["git", f"--git-dir={repo_git}", "diff", rev_buggy, rev_fixed])


def trigger_tests(pid: str, bug_id: str) -> list[str]:
    f = PROJ / pid / "trigger_tests" / bug_id
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("--- "):
            out.append(line[4:].strip())
    return out


def main() -> int:
    import pandas as pd
    from common import load_index, save_index

    df = load_index()
    mask = df["source"] == "defects4j"
    d4j_rows = df[mask]
    print(f"defects4j 待回填 {len(d4j_rows)} 题")

    updated, sealed, failed = 0, 0, []
    for sid, (pid, bug_id) in {
        r["source_id"]: tuple(r["source_id"].rsplit("-", 1)) for _, r in d4j_rows.iterrows()
    }.items():
        cdb = PROJ / pid / "commit-db"
        if not cdb.exists():
            failed.append((sid, "no commit-db")); continue
        hit = None
        for line in cdb.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split(",")
            if len(parts) >= 4 and parts[0] == bug_id:
                hit = parts; break
        if not hit:
            failed.append((sid, "bug 不在 commit-db")); continue
        # commit-db 列序（v3 实测）: bug_id, revision_FIXED, revision_BUGGY
        # 证据: checkout -v 1b 检出的是第 3 列 d1a45e97
        _, rev_f, rev_b, issue_id, issue_url = (hit + [""] * 5)[:5]
        repo_git = BUNDLES / f"{PID_TO_REPO.get(pid, pid)}.git"
        if not repo_git.exists():
            failed.append((sid, f"无 bundle {repo_git.name}")); continue
        patch = gold_diff(repo_git, rev_b, rev_f)
        tests = trigger_tests(pid, bug_id)
        if not patch or not tests:
            failed.append((sid, "diff或触发测试为空")); continue
        row_mask = mask & (df["source_id"] == sid)
        bank_id = df.loc[row_mask, "bank_id"].iloc[0]
        df.loc[row_mask, "fail_to_pass"] = __import__("json").dumps(tests, ensure_ascii=False)
        df.loc[row_mask, "source_url"] = issue_url
        df.loc[row_mask, "notes"] = "java_local"
        seal(bank_id, patch)
        sealed += 1
        updated += row_mask.sum()
        if sealed % 100 == 0:
            print(f"  进度 {sealed}")

    save_index(df)
    print(f"回填完成: 金标封存 {sealed} 题, 失败 {len(failed)}")
    for sid, why in failed[:10]:
        print(f"  [失败] {sid}: {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
