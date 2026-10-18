"""CLI 适配器：把本地 CLI agent 工具当黑盒选手拉起。

支持的工具（按 workdir 里可执行文件自动探测，也可 --tool 指定）：
  claude   Claude Code:  claude -p "<题面>" --permission-mode acceptEdits
  codex    Codex CLI:    codex exec "<题面>"
工作目录 = 剥好 VCS 的代码快照（物化时已 git init 单提交基线），
结束后 git diff 收 patch，全程计时。
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path


def detect_tool(workdir: Path) -> str | None:
    for t in ("claude", "codex", "zcode"):
        if shutil.which(t):
            return t
    return None


def run_cli(task_dir: Path, workdir: Path, problem_md: str, tool: str | None,
            timeout_sec: int = 1800) -> dict:
    """workdir 必须已 git init（单提交基线）。返回 {patch, stdout, elapsed_sec, tool}。"""
    tool = tool or detect_tool(workdir)
    if not tool:
        return {"error": "未找到可用的 CLI agent（claude/codex/zcode 均不在 PATH）"}

    prompt = problem_md + "\n\n请直接修改当前目录中的代码完成修复。不要创建新仓库，不要改动测试文件。"
    if tool == "claude":
        cmd = ["claude", "-p", prompt, "--permission-mode", "acceptEdits", "--max-turns", "40"]
    elif tool == "codex":
        cmd = ["codex", "exec", "--full-auto", prompt]
    else:
        cmd = [tool, "exec", prompt]

    t0 = time.time()
    try:
        cp = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True,
                            timeout=timeout_sec, encoding="utf-8", errors="replace")
        elapsed = round(time.time() - t0, 1)
        stdout = (cp.stdout or "") + "\n--stderr--\n" + (cp.stderr or "")
    except subprocess.TimeoutExpired:
        return {"error": f"超时 {timeout_sec}s", "elapsed_sec": timeout_sec, "tool": tool}

    diff = subprocess.run(["git", "diff", "HEAD"], cwd=workdir,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    patch = diff.stdout.strip()
    return {"patch": patch, "stdout": stdout[-8000:], "elapsed_sec": elapsed, "tool": tool}
