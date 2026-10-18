"""工作区物化：为某题生成 agent 的工作目录 bank/tasks/<bank_id>/workspace/。

docker_image 源：docker create → docker cp /testbed（或 /repo）→ strip_vcs → git init 单提交基线
（重建全新 git 是为了之后 git diff 收 patch；历史为空，无作弊面）
docker_build/其他：标记 pending，待环境层就绪。

用法: uv run python tools/prepare_workspace.py --report logs/last_pull_report.json
      uv run python tools/prepare_workspace.py --bank-ids SWR2-000001
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from strip_vcs import strip  # noqa: E402


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", **kw)


def materialize_from_image(image_ref: str, workdir: Path) -> str:
    """从镜像导出代码。尝试常见路径 /testbed → /repo → /。"""
    cid = sh(["docker", "create", image_ref]).stdout.strip()
    if not cid:
        return "create_failed"
    try:
        for src in ("/testbed", "/repo", "/app"):
            cp = sh(["docker", "cp", f"{cid}:{src}", str(workdir)])
            if cp.returncode == 0:
                return "ok"
        # 兜底：整根文件系统导出（慢，但保底）
        cp = sh(["docker", "cp", f"{cid}:/", str(workdir)])
        return "ok_root" if cp.returncode == 0 else "copy_failed"
    finally:
        sh(["docker", "rm", "-f", cid])


def git_baseline(workdir: Path) -> bool:
    s1 = sh(["git", "init", "-q"], cwd=workdir)
    s2 = sh(["git", "add", "-A"], cwd=workdir)
    s3 = sh(["git", "-c", "user.email=bank@local", "-c", "user.name=bank",
             "commit", "-qm", "baseline"], cwd=workdir)
    return s1.returncode == 0 and s2.returncode == 0 and s3.returncode == 0


def prepare(task: dict) -> str:
    from env import TASKS
    workdir = TASKS / task["bank_id"] / "workspace"
    if (workdir / ".git").exists():
        return "cached"
    if task["env_type"] != "docker_image" or not task.get("image_ref"):
        return f"pending({task['env_type']})"
    workdir.mkdir(parents=True, exist_ok=True)
    state = materialize_from_image(task["image_ref"], workdir)
    if state not in ("ok", "ok_root"):
        return state
    # /testbed 内容可能套在 workdir/testbed/ 下，视情况上提
    inner = workdir / "testbed"
    if inner.is_dir() and not (workdir / ".git").exists():
        for p in inner.iterdir():
            p.rename(workdir / p.name)
        inner.rmdir()
    strip(workdir)
    return "ready" if git_baseline(workdir) else "git_failed"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default=None)
    ap.add_argument("--bank-ids", default=None)
    args = ap.parse_args()

    import pandas as pd
    from common import load_index

    if args.bank_ids:
        ids = args.bank_ids.split(",")
    elif args.report:
        ids = [r["bank_id"] for r in json.loads(Path(args.report).read_text(encoding="utf-8"))]
    else:
        print("需要 --report 或 --bank-ids")
        return 1
    df = load_index()
    for _, t in df[df["bank_id"].isin(ids)].iterrows():
        state = prepare(t.to_dict())
        print(f"  {t['bank_id']:<14} {t['source']:<18} {state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
