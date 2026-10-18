"""BIP 工作区去污染：patch -R 反向应用密封补丁，把审计写入的金标还原成 buggy 原版。

背景（2026-10-16 事故）：bip_gold_audit 判分后不恢复工作区，模型批 prompt 从工作区读文件
→ 58 题读到金标答案。本工具逆向还原；幂等（先查状态再动手）；失败兜底=清工作区重物化。

校验口径：密封补丁里有真实 hunk 的文件，工作区内容必须 ≠ fixed 快照内容
（相等 = 没还原成功，或被二次逆向转回 fixed——同一信号都能抓到）。
用法: D:/tikutaku/venv/Scripts/python.exe tools/bip_sanitize_workdirs.py [view 或逗号分隔 id]
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "tools"))

import scoring  # noqa: E402
from seal_gold import unseal  # noqa: E402

TASKS = scoring.BIP_TASKS_ROOT
FIXED = scoring.DATA_ROOT / "bip_fixed"


def hunk_files(text: str) -> set[str]:
    """补丁里有真实 hunk 的源码文件（略过纯文件头；测试文件不参与——
    工作区测试文件按 BIP 官方流程就该保持 fixed 版）。"""
    out, cur = set(), None
    for line in text.splitlines():
        m = re.match(r"diff --git a/(\S+) b/(\S+)", line)
        if m:
            cur = m.group(2)
            continue
        if line.startswith("@@") and cur:
            if not scoring._is_test_path(cur):
                out.add(cur)
            cur = None
    return out


def norm(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")


def verify(wd: Path, bid: str, files: set[str]) -> list[str]:
    """返回仍等于 fixed 内容的文件清单（应为空）。"""
    bad = []
    for p in files:
        a, b = wd / p, FIXED / bid / p
        if a.exists() and b.exists() and norm(a.read_text(encoding="latin-1")) == \
                norm(b.read_text(encoding="latin-1")):
            bad.append(p)
    return bad


def sanitize_one(bid: str) -> str:
    wd = TASKS / bid
    if not wd.exists() or not any(wd.iterdir()):
        return "nowd"
    text = unseal(bid)
    files = hunk_files(text)
    if not files or not (FIXED / bid).exists():
        return "nofixed"
    if not verify(wd, bid, files):
        return "already"  # 已还原（或本就 buggy），幂等跳过
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False, encoding="utf-8",
                                     newline="") as tf:
        tf.write(text)
        pf = Path(tf.name)
    try:
        cp = subprocess.run(["patch", "-R", "-p1", "--ignore-whitespace", "-f",
                             "--no-backup-if-mismatch", "-i", str(pf)],
                            cwd=str(wd), capture_output=True, text=True, timeout=120)
    finally:
        pf.unlink(missing_ok=True)
    rejs = list(wd.rglob("*.rej")) + list(wd.rglob("*.orig"))
    for r in rejs:
        r.unlink(missing_ok=True)
    if cp.returncode != 0 or rejs or verify(wd, bid, files):
        # 兜底：清工作区，下次 bip_checkout 从 tarball 全新物化（字节级保真）
        shutil.rmtree(wd)
        return "wipe"
    (wd / ".tikutaku_sanitized").write_text("2026-10-16\n", encoding="utf-8")
    return "ok"


def main() -> int:
    spec = sys.argv[1] if len(sys.argv) > 1 else "bip_pandas"
    if spec.startswith("BIP-"):
        ids = spec.split(",")
    else:
        ids = json.loads((ROOT / "bank" / "views" / f"{spec}.json").read_text(encoding="utf-8"))
    stat: dict[str, list] = {}
    for bid in ids:
        st = sanitize_one(bid)
        stat.setdefault(st, []).append(bid)
        if st == "wipe":
            print(f"{bid}: 逆向失败 → 工作区已清（下次运行重新物化）", flush=True)
    for k in sorted(stat):
        print(f"{k}: {len(stat[k])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
