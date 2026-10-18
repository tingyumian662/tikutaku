"""重封完整金标：BugsInPy 的 bug_patch.txt 是上游修复的严格子集（漏 .pyx 配套 / 漏兄弟 .py 文件）。

实证（2026-10-16）：BIP-000227/235/291 补丁漏 _libs 的 .pyx 改动（.so 永远 buggy 行为）、
BIP-000231/289/337 漏配套 .py（单文件金标塞进 buggy 树造成接口撕裂 ImportError）。
修复：buggy+fixed 双快照 → 包内源码全量 diff → 以完整 unified diff 重写 seal。

buggy 参照优先用已物化的工作区（筛查后源码未被改过），省 tarball 重下；
工作区金标路径若已等于 fixed（被审计污染过）则判定脏 → 丢弃工作区走全新下载。

用法: D:/tikutaku/venv/Scripts/python.exe tools/bip_reseal_complete.py BIP-000227,...
"""
from __future__ import annotations

import difflib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "tools"))

import scoring  # noqa: E402
from bip_gold_audit import fetch_tar  # noqa: E402
from seal_gold import seal  # noqa: E402

RESEAL_ROOT = scoring.DATA_ROOT / "bip_reseal"
# versioneer 产物随 commit 变，非修复内容；.c 是 Cython 生成物（判分时增量再生成）
EXCLUDE_NAMES = {"_version.py"}


def _read(p: Path) -> str:
    """读文件；WSL 建的 DrvFs 符号链接 Windows 侧 open 会 Errno 22，回退 WSL cat。"""
    try:
        return p.read_text(encoding="latin-1")
    except OSError:
        import subprocess
        cp = subprocess.run(["wsl", "-d", "tikutaku", "--", "cat", _wslp(p)],
                            capture_output=True, timeout=60)
        return cp.stdout.decode("latin-1", errors="replace")


def _wslp(p: Path) -> str:
    s = str(p).replace("\\", "/")
    return f"/mnt/{s[0].lower()}{s[2:]}"


def diff_paths(buggy: Path, fixed: Path, pkg: str) -> list[str]:
    out = []
    files = set()
    for base in (buggy, fixed):
        for p in (base / pkg).rglob("*"):
            if p.suffix in (".py", ".pyx"):
                files.add(p.relative_to(base).as_posix())
    for rel in sorted(files):
        if Path(rel).name in EXCLUDE_NAMES or scoring._is_test_path(rel):
            continue
        a = _read(buggy / rel) if (buggy / rel).exists() else ""
        b = _read(fixed / rel) if (fixed / rel).exists() else ""
        if a != b:
            out.append(rel)
    return out


def make_patch(buggy: Path, fixed: Path, paths: list[str]) -> str:
    parts = []
    for rel in paths:
        a = _read(buggy / rel)
        b = _read(fixed / rel)
        if a == b:
            continue
        u = difflib.unified_diff(a.splitlines(keepends=True), b.splitlines(keepends=True),
                                 fromfile=f"a/{rel}", tofile=f"b/{rel}")
        parts.append(f"diff --git a/{rel} b/{rel}\n" + "".join(u))
    return "".join(parts)


def _norm(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")


def workdir_usable(wd: Path, bid: str, paths: list[str], fixed: Path) -> bool:
    """工作区可作 buggy 参照：金标路径源码必须 ≠ fixed（未被审计/模型污染）。"""
    for p in paths:
        a, b = wd / p, fixed / p
        if a.exists() and b.exists() and _norm(_read(a)) == _norm(_read(b)):
            return False
    return True


def main() -> int:
    import pandas as pd

    ids = sys.argv[1].split(",")
    df = pd.read_parquet(ROOT / "bank" / "index.parquet")
    for _, r in df[df.bank_id.isin(ids)].iterrows():
        env = json.loads(r.env_notes)
        old_paths = scoring.d4j_gold_paths(r.bank_id)
        pkg = old_paths[0].split("/")[0] if old_paths else str(r.repo)
        fixed = fetch_tar(env["repo_url"], env["fixed_commit"],
                          scoring.DATA_ROOT / "bip_fixed" / r.bank_id)
        if not fixed:
            print(f"{r.bank_id} fixed_fetch_fail")
            continue
        wd = scoring.BIP_TASKS_ROOT / r.bank_id
        # buggy 参照：工作区存在且未被污染 → 直接用（筛查后源码是原版），省 tarball 重下
        dirty = bool(old_paths) and wd.exists() and any(wd.iterdir()) and not workdir_usable(
            wd, r.bank_id, [p for p in old_paths if not scoring._is_test_path(p)], fixed)
        buggy = None
        if not dirty and wd.exists() and any(wd.iterdir()):
            buggy = wd
        else:
            buggy = fetch_tar(env["repo_url"], env["buggy_commit"],
                              RESEAL_ROOT / f"{r.bank_id}_buggy")
        if not buggy:
            print(f"{r.bank_id} buggy_fetch_fail")
            continue
        new = diff_paths(buggy, fixed, pkg)
        added = [p for p in new if p not in old_paths]
        # 膨胀护栏：完整 diff 比 bug_patch 多出 8 个文件以上 = buggy/fixed 跨了无关
        # 大改（如 ansible 模块改名潮 298 文件），完整态当金标会让模型 prompt 爆炸。
        # 此类题保持原补丁（审计已实证可通过）；只有小幅缺伴（≤8）才补全。
        if len(added) > 8:
            print(f"{r.bank_id} 跨度膨胀 {len(added)} 文件，保持原补丁")
            continue
        paths = [p for p in old_paths if Path(p).name not in EXCLUDE_NAMES] + added
        if sorted(paths) == sorted(old_paths):
            print(f"{r.bank_id} 补丁已完整，跳过")
            continue
        seal(r.bank_id, make_patch(buggy, fixed, paths))
        if dirty:
            shutil.rmtree(wd)
            print(f"{r.bank_id} 重封 {len(old_paths)}→{len(paths)} 文件：+{added}；工作区脏已清")
        else:
            print(f"{r.bank_id} 重封 {len(old_paths)}→{len(paths)} 文件：+{added}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
