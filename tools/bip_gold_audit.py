"""BIP 金标批量预检：codeload 物化 buggy + fixed → 金标文件全文 → WSL pytest 判分 → 应全 pass。

用法: D:/tikutaku/venv/Scripts/python.exe tools/bip_gold_audit.py <bank_id,...或项目名>
"""
from __future__ import annotations

import json
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "tools"))

import scoring  # noqa: E402
from seal_gold import unseal  # noqa: E402

FIXED_ROOT = scoring.DATA_ROOT / "bip_fixed"


def fetch_tar(repo_url: str, commit: str, dest: Path) -> Path | None:
    if dest.exists():  # fixed 快照不可变，缓存命中直接用（codeload 代理不稳，别重拉）
        return dest
    repo = repo_url.rstrip("/").split("github.com/")[1]
    url = f"https://codeload.github.com/{repo}/tar.gz/{commit}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tar = dest.parent / f"{dest.name}.tar.gz"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "tikutaku"})
        with urllib.request.urlopen(req, timeout=180) as r, open(tar, "wb") as f:
            f.write(r.read())
        with tarfile.open(tar) as tf:
            root = tf.getmembers()[0].name.split("/")[0]
            tf.extractall(dest.parent, filter="tar")
        import shutil as _sh
        if dest.exists():
            _sh.rmtree(dest)
        (dest.parent / root).rename(dest)
        return dest
    except Exception as e:  # noqa: BLE001
        print(f"    fetch_fail {commit[:8]}: {type(e).__name__} {str(e)[:80]}")
        return None
    finally:
        tar.unlink(missing_ok=True)


def audit_one(r, env: dict, f2p: list) -> dict:
    bid = r.bank_id
    wd = scoring.bip_checkout(bid, env.get("repo_url", ""), env.get("buggy_commit", ""))
    if wd is None:
        return {"verdict": "checkout_fail"}
    # 金标涉及文件（从 seal 提取路径；全文取自 fixed tarball）
    paths = [p for p in scoring.d4j_gold_paths(bid) if not scoring._is_test_path(p)]
    fixed = fetch_tar(env["repo_url"], env["fixed_commit"], FIXED_ROOT / bid)
    patch_files = {}
    if fixed:
        for p in paths:
            fp = fixed / p
            if fp.exists():
                patch_files[p] = fp.read_text(encoding="latin-1")
    if not patch_files:
        return {"verdict": "gold_empty", "paths": paths[:3]}
    venv = scoring.bip_venv(str(r.repo), Path(env["bug_dir"]) / "requirements.txt"
                            if env.get("bug_dir") else None, bank_id=bid)
    if venv is None:
        return {"verdict": "venv_fail"}
    # F2P 测试方法只在 fixed 版存在：用 fixed 的测试文件覆盖工作区（BIP 官方流程）
    scoring.bip_prepare_test(wd, bid, env["repo_url"], env["fixed_commit"], Path(env["bug_dir"]))
    # BIP 官方语义：判分前在 workdir 跑 bug_dir/setup.sh（venv 中）；部分题无 setup.sh。
    # TIKU_SKIP_SETUP=1 跳过（DrvFs 上 install 拷文件慢；导入靠 PYTHONPATH+懒编译已兜住）
    import os
    setup_sh = Path(env['bug_dir']) / 'setup.sh'
    setup = (f"bash {_wsl(setup_sh)}"
             if setup_sh.exists() and os.environ.get("TIKU_SKIP_SETUP") != "1" else '')
    # BIP workdir 无 git 不能自愈：审计写入的金标必须还原 buggy 原版，
    # 否则模型批从工作区读 prompt 会直接看到答案（2026-10-16 污染事故根因）
    orig = {p: (wd / p).read_text(encoding="latin-1") for p in patch_files if (wd / p).exists()}
    try:
        return scoring.score_bip(wd, patch_files, f2p, venv, timeout_sec=2400, cwd_setup=setup)
    finally:
        for p, c in orig.items():
            fp = wd / p
            if fp.exists():
                fp.write_text(c, encoding="latin-1", newline="\n")


def main() -> int:
    import pandas as pd

    spec = sys.argv[1] if len(sys.argv) > 1 else "tqdm"
    df = pd.read_parquet(ROOT / "bank" / "index.parquet")
    bip = df[df.source == "bugsinpy"]
    if spec.startswith("BIP-"):
        rows = bip[bip.bank_id.isin(spec.split(","))]
    else:
        rows = bip[bip.repo == spec].head(3)
    results = {}
    for _, r in rows.iterrows():
        t0 = time.time()
        try:
            env = json.loads(r.env_notes)
        except Exception:
            env = {}
        f2p = json.loads(r.fail_to_pass or "[]")
        try:
            v = audit_one(r, env, f2p)
        except Exception as e:  # noqa: BLE001
            v = {"verdict": f"error:{type(e).__name__}", "detail": str(e)[:200]}
        v["source_id"] = r.source_id
        results[r.bank_id] = v
        print(f"{r.source_id:<16} {v.get('verdict','?'):<22} {v.get('sec', round(time.time()-t0,1))}s "
              f"{str(v.get('tail', v.get('detail','')))[:110]}", flush=True)
    ok = sum(1 for v in results.values() if v.get("verdict") == "pass")
    print(f"\n金标预检 {ok}/{len(results)} pass")
    return 0 if ok == len(results) else 1


def _wsl(p: Path) -> str:
    return scoring._wsl_path(p)


if __name__ == "__main__":
    sys.exit(main())
