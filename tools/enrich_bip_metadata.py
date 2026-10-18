"""BugsInPy 元数据补强：bug.info/run_test.sh → index（fail_to_pass/env_notes/verifier_type）。

用法:
  D:/tikutaku/venv/Scripts/python.exe tools/enrich_bip_metadata.py [--with-msg]
  --with-msg: 额外拉 GitHub API 的 fixed commit message 补题面（匿名限 60/h，分批跑）
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIP = ROOT / "sources" / "repos" / "bugsinpy" / "projects"


def parse_bug_info(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        m = re.match(r'(\w+)\s*=\s*"([^"]*)"', line.strip())  # youtube-dl 的键值等号两侧带空格
        if m:
            out[m.group(1)] = m.group(2)
    return out


def pytest_nodes(cmd: str) -> list[str]:
    """run_test.sh 测试命令 → 节点列表。支持 pytest / py.test / tox（取其 pytest 节点）/
    python(3) -m pytest / python -m unittest -q module.Class.method。"""
    cmd = cmd.strip()
    m = re.match(r"python3?\s+-m\s+pytest\s+(.*)", cmd)
    if m:
        return [t for t in m.group(1).split() if "::" in t or t.endswith(".py")]
    if cmd.startswith(("pytest", "py.test")):
        rest = cmd.split(None, 1)[1] if " " in cmd else ""
        return [t for t in rest.split() if "::" in t or t.endswith(".py")]
    if cmd.startswith("tox"):
        return [t for t in cmd.split()[1:] if "::" in t or t.endswith(".py")]
    m = re.match(r"python3?\s+-m\s+unittest\s+(?:-\S+\s+)*(\S+)", cmd)
    if m and "." in m.group(1):
        return [m.group(1)]
    return []


def fetch_msg(repo_url: str, sha: str) -> str:
    api = f"https://api.github.com/repos/{repo_url.split('github.com/')[1].rstrip('/')}/commits/{sha}"
    req = urllib.request.Request(api, headers={"User-Agent": "tikutaku"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())["commit"]["message"]
    except Exception as e:  # noqa: BLE001
        return f"__fetch_fail__:{type(e).__name__}"


def main() -> int:
    import pandas as pd

    with_msg = "--with-msg" in sys.argv
    df = pd.read_parquet(ROOT / "bank" / "index.parquet")
    m = df["source"] == "bugsinpy"
    bip_ids = {f"{sid}": i for i, sid in zip(df.index[m], df.loc[m, "source_id"])}

    texts = df["problem_statement"].tolist()
    f2ps = df["fail_to_pass"].tolist()
    notes_l = df["notes"].tolist()
    vf = df["verifier_type"].astype(object).where(~m, "bip_tests")
    stat = {"ok": 0, "no_dir": 0, "no_test": 0, "msg_fetch": 0, "msg_fail": 0}

    for proj_dir in sorted(BIP.iterdir()):
        if not (proj_dir / "bugs").is_dir():
            continue
        pinfo = parse_bug_info((proj_dir / "project.info").read_text(encoding="utf-8", errors="replace"))
        repo_url = pinfo.get("github_url", "")
        for bug_dir in sorted(proj_dir.joinpath("bugs").iterdir()):
            src_id = f"{proj_dir.name}-{bug_dir.name}"
            i = bip_ids.get(src_id)
            if i is None:
                continue
            bi = parse_bug_info(bug_dir.joinpath("bug.info").read_text(encoding="utf-8", errors="replace"))
            run_sh = bug_dir.joinpath("run_test.sh")
            nodes = pytest_nodes(run_sh.read_text(encoding="utf-8", errors="replace")) if run_sh.exists() else []
            if not nodes:
                stat["no_test"] += 1
                notes_l[i] = (notes_l[i] or "") + ";bip_no_test_cmd"
                continue
            f2ps[i] = json.dumps(nodes)
            env = {"buggy_commit": bi.get("buggy_commit_id"), "fixed_commit": bi.get("fixed_commit_id"),
                   "python_version": bi.get("python_version"), "repo_url": repo_url,
                   "bug_dir": str(bug_dir.relative_to(ROOT)).replace("\\", "/"),
                   "setup": "sh setup.sh（bug_dir 内，venv 中执行）"}
            df.iat[i, df.columns.get_loc("env_notes")] = json.dumps(env, ensure_ascii=False)
            if with_msg:
                msg = fetch_msg(repo_url, bi.get("fixed_commit_id", ""))
                if msg.startswith("__fetch_fail__"):
                    stat["msg_fail"] += 1
                    time.sleep(1.5)
                else:
                    stat["msg_fetch"] += 1
                    texts[i] = (f"修复 {proj_dir.name} 中的 bug（fixed commit {bi.get('fixed_commit_id','')[:10]}）。\n\n"
                                f"缺陷修复提交说明（据此推断并修复 bug）：\n{msg[:1500]}\n\n"
                                f"请修复该缺陷，使触发测试通过。")
                    time.sleep(1.2)
            stat["ok"] += 1
    print("stat:", stat)
    df["problem_statement"] = texts
    df["fail_to_pass"] = f2ps
    df["notes"] = notes_l
    df["verifier_type"] = vf
    bak = ROOT / "bank" / "index.parquet.bak2"
    shutil.copy2(ROOT / "bank" / "index.parquet", bak)
    df.to_parquet(ROOT / "bank" / "index.parquet", index=False)
    ok = df[m & (df["verifier_type"] == "bip_tests") & df["fail_to_pass"].map(lambda s: bool(json.loads(s) if s else []))]
    print(f"BIP 有效题（fail_to_pass 非空）: {len(ok)} / {int(m.sum())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
