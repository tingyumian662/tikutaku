"""代码快照清洗：从题目环境导出代码时剥离一切 VCS 痕迹。

SWE-rebench 实证过 agent 会翻 .git 历史直接抄官方修复 patch。
物化题目目录时必须过这道工序：
  - 删 .git/.hg/.svn 目录
  - 删 .git 文件（worktree 文件）、.gitignore 不动（无害）
  - 顺带删常见缓存：__pycache__/.pytest_cache/node_modules/.venv
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

VCS_DIRS = {".git", ".hg", ".svn", ".bzr"}
VCS_FILES = {".git", ".gitmodules", ".gitattributes"}   # .git 文件级（worktree 指针）
CACHE_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", ".venv", ".tox"}


def strip(root: Path, also_cache: bool = True) -> int:
    """删除 root 下所有 VCS 痕迹，返回删除项数量。"""
    removed = 0
    if not root.exists():
        return 0
    for p in list(root.rglob("*")):
        name = p.name
        if p.is_dir() and (name in VCS_DIRS or (also_cache and name in CACHE_DIRS)):
            shutil.rmtree(p, ignore_errors=True)
            removed += 1
        elif p.is_file() and name in VCS_FILES and p.parent != root:
            p.unlink(missing_ok=True)
            removed += 1
    # 根目录的 .git 可能是目录或文件
    git_root = root / ".git"
    if git_root.is_dir():
        shutil.rmtree(git_root, ignore_errors=True)
        removed += 1
    elif git_root.is_file():
        git_root.unlink(missing_ok=True)
        removed += 1
    return removed


if __name__ == "__main__":
    target = Path(sys.argv[1]).resolve()
    n = strip(target)
    print(f"已清除 {n} 处 VCS/缓存痕迹: {target}")
