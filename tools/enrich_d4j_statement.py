"""D4J 题面补强 + 池注册：占位符题面 → fix commit message + JIRA 引用。

同时完成：
  1. verifier_type: f2p_p2p_tests → d4j_tests（802 有效题，进可测池）
  2. retired=True: 50 空壳金标（enrich_pending）+ 2 checkout_fail
  3. problem_statement 用本地 repo bundle 的 fix commit message 补强（零网络）

用法: uv run python tools/enrich_d4j_statement.py [--dry]
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

D4J = ROOT / "sources" / "repos" / "defects4j"
PROJECTS = D4J / "framework" / "projects"
REPOS = D4J / "project_repos"

PID_TO_GIT = {
    "Chart": "jfreechart.git", "Cli": "commons-cli.git", "Closure": "closure-compiler.git",
    "Codec": "commons-codec.git", "Collections": "commons-collections.git",
    "Compress": "commons-compress.git", "Csv": "commons-csv.git", "Gson": "gson.git",
    "JacksonCore": "jackson-core.git", "JacksonDatabind": "jackson-databind.git",
    "JacksonXml": "jackson-dataformat-xml.git", "Jsoup": "jsoup.git",
    "JxPath": "commons-jxpath.git", "Lang": "commons-lang.git", "Math": "commons-math.git",
    "Mockito": "mockito.git", "Time": "joda-time.git",
}

_NOISE = re.compile(r"^(git-svn-id:|Change-Id:|Reviewed-by:|Signed-off-by:|PiperOrigin-RevId:|Review-Url:|Bug: )", re.I)


def load_commit_dbs() -> dict[tuple[str, str], tuple[str, str, str]]:
    """(pid, bug_id) -> (fix_commit, jira, url)"""
    out = {}
    for cdb in PROJECTS.glob("*/commit-db"):
        pid = cdb.parent.name
        for line in cdb.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.strip().split(",")
            if len(parts) >= 4:
                # 列序: bug_id, fix_sha, buggy_sha, issue_id, issue_url
                out[(pid, parts[0])] = (parts[1], parts[3], parts[4] if len(parts) > 4 else "")
    return out


def commit_message(git_dir: Path, sha: str) -> str:
    try:
        cp = subprocess.run(["git", "--git-dir", str(git_dir), "log", "-1", "--format=%B", sha],
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=30)
    except Exception:
        return ""
    lines = [l.rstrip() for l in (cp.stdout or "").splitlines()
             if l.strip() and not _NOISE.match(l.strip())]
    return "\n".join(lines).strip()


def main() -> int:
    import pandas as pd

    dry = "--dry" in sys.argv
    df = pd.read_parquet(ROOT / "bank" / "index.parquet")
    m = df["source"] == "defects4j"
    print(f"D4J 共 {int(m.sum())} 题")

    # 1) retired：空壳金标 + checkout_fail
    retired_mask = m & (df["notes"].fillna("").str.startswith("enrich_pending")
                        | (df["notes"] == "java_checkout_fail"))
    print(f"retired 标记: {int(retired_mask.sum())} 题（空壳金标/checkout 失败）")

    # 2) verifier_type → d4j_tests（全部 D4J；池子由 retired+fail_to_pass 过滤）
    vf = df["verifier_type"].astype(object).where(~m, "d4j_tests")

    # 3) 题面补强
    dbs = load_commit_dbs()
    stat = {"enriched": 0, "empty_msg": 0, "no_db": 0}
    texts, new_notes = df["problem_statement"].tolist(), df["notes"].tolist()
    rows = df.index[m].tolist()
    for i in rows:
        src_id = df.at[i, "source_id"]
        pid, _, bug = src_id.rpartition("-")
        git_dir = REPOS / PID_TO_GIT.get(pid, "")
        entry = dbs.get((pid, bug))
        if not entry or not git_dir.exists():
            stat["no_db"] += 1
            continue
        fix_sha, jira, url = entry
        msg = commit_message(git_dir, fix_sha)
        if not msg:
            stat["empty_msg"] += 1
            continue
        jira_line = f"（issue: {jira}，{url}）" if url else f"（issue: {jira}）"
        texts[i] = (f"修复 {pid} 项目中的缺陷 {jira_line}。\n\n"
                    f"缺陷修复提交说明（描述了修复所做的事，据此推断并修复 bug）：\n"
                    f"{msg[:2000]}\n\n"
                    f"请修复该缺陷，使触发测试通过。")
        stat["enriched"] += 1
    print("题面补强:", stat)

    if dry:
        print("--dry 跳过写回")
        return 0
    df["problem_statement"] = texts
    df["verifier_type"] = vf
    df["retired"] = df["retired"] | retired_mask
    bak = ROOT / "bank" / "index.parquet.bak"
    shutil.copy2(ROOT / "bank" / "index.parquet", bak)
    df.to_parquet(ROOT / "bank" / "index.parquet", index=False)
    print(f"已写回（备份: {bak.name}）")

    # 池子预览
    ok = df[m & (df["verifier_type"] == "d4j_tests") & df["fail_to_pass"].notna() & (~df["retired"])]
    print(f"D4J 入池: {len(ok)} 题")
    return 0


if __name__ == "__main__":
    sys.exit(main())
