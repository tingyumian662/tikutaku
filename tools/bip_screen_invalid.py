"""BIP 无效题筛查：buggy 版直接跑 F2P 测试——也 PASS 的题在 Linux 上无区分度
（Windows 特有 bug 等），标 notes=invalid_on_linux，首测/入池剔除。

用法: D:/tikutaku/venv/Scripts/python.exe tools/bip_screen_invalid.py <项目名或BIP-ID列表>
仅筛 venv 已就绪的项目（bip_venv 幂等，未建的会现场建——耗时）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "tools"))

import scoring  # noqa: E402


def screen_one(r, env: dict) -> dict:
    bid = r.bank_id
    wd = scoring.bip_checkout(bid, env.get("repo_url", ""), env.get("buggy_commit", ""))
    if wd is None:
        return {"verdict": "checkout_fail"}
    scoring.bip_prepare_test(wd, bid, env.get("repo_url", ""), env.get("fixed_commit", ""),
                             Path(env["bug_dir"]) if env.get("bug_dir") else None)
    venv = scoring.bip_venv(str(r.repo), Path(env["bug_dir"]) / "requirements.txt"
                            if env.get("bug_dir") else None, bank_id=bid)
    if venv is None:
        return {"verdict": "venv_fail"}
    setup_sh = Path(env["bug_dir"]) / "setup.sh"
    import os
    setup = (f"bash {scoring._wsl_path(setup_sh)}"
             if setup_sh.exists() and os.environ.get("TIKU_SKIP_SETUP") != "1" else "")
    # 绕过 score_bip 的 patch_files 非空检查：直接判分脚本跑测试，pass = 无效题
    def _to_pytest(n: str) -> str:
        if "::" in n or n.endswith(".py"):
            return n
        parts = n.split(".")
        if len(parts) >= 3:
            return "/".join(parts[:-2]) + ".py::" + "::".join(parts[-2:])
        return n
    f2p = json.loads(r.fail_to_pass or "[]")
    nodes = " ".join(f"'{_to_pytest(n)}'" for n in f2p)
    wd_w = scoring._wsl_path(wd)
    script = (
        "#!/bin/bash\n"
        f'cd {wd_w} && export PATH="{venv}/bin:$PATH" && '
        'export PYTHONPATH="$PWD:$PWD/lib:$PWD/src:$PYTHONPATH"; '
        'export MPLBACKEND=Agg; '
        'export PYTHONWARNINGS=ignore::DeprecationWarning; '
        'command -v gcc-10 >/dev/null && export CC=gcc-10; '
        'if [ -f setup.py ] && [ ! -f .tikutaku_built ]; then '
        'python setup.py build_ext --inplace -j 0 >/dev/null 2>&1 && touch .tikutaku_built; fi; '
        + (f"{setup} && " if setup else "") + "\n"
        f"{venv}/bin/python -m pytest -q -W ignore::DeprecationWarning -W ignore::PendingDeprecationWarning {nodes} > /tmp/bip_screen_$$.txt 2>&1\n"
        "rc=$?\n"
        "tail -2 /tmp/bip_screen_$$.txt\n"
        "rm -f /tmp/bip_screen_$$.txt\n"
        "exit $rc\n"
    )
    import uuid
    sf = scoring.BIP_TASKS_ROOT / f"_screen_{uuid.uuid4().hex[:8]}.sh"
    sf.write_text(script, encoding="utf-8", newline="\n")
    import subprocess
    cp = scoring._wsl_run(f"bash {scoring._wsl_path(sf)}", 1500)
    body = ((cp.stdout or "") + (cp.stderr or ""))[-200:].replace("\n", " ")
    if cp.returncode == 0:
        return {"verdict": "INVALID_buggy_also_passes", "tail": body[:120]}
    # python -m pytest 缺模块时退出码也是 1（不是 2）——必须显式排除，否则残次 venv 全部假阳性
    if "No module named" in body or "ModuleNotFoundError" in body or "ImportError" in body:
        return {"verdict": "env_error", "tail": body[:120]}
    # pytest 退出码判据：1=测试真失败（buggy 应有的行为）；
    # 2/3/4/5=收集/内部/用法/空集——环境问题（日志里的 "ERROR" 字样不代表收集错）
    if cp.returncode == 1:
        return {"verdict": "ok_buggy_fails", "tail": body[:80]}
    return {"verdict": "env_error", "tail": body[:120]}


def main() -> int:
    import pandas as pd

    spec = sys.argv[1] if len(sys.argv) > 1 else "black"
    df = pd.read_parquet(ROOT / "bank" / "index.parquet")
    bip = df[df.source == "bugsinpy"]
    if spec.startswith("BIP-"):
        rows = bip[bip.bank_id.isin(spec.split(","))]
    else:
        rows = bip[bip.repo == spec]
    out = {}
    log = ROOT / "logs" / "bip_screen.json"
    if log.exists():
        out = json.loads(log.read_text(encoding="utf-8"))
    for _, r in rows.iterrows():
        try:
            env = json.loads(r.env_notes)
        except Exception:
            env = {}
        try:
            res = screen_one(r, env)
        except Exception as e:  # noqa: BLE001
            res = {"verdict": f"error:{type(e).__name__}", "tail": str(e)[:120]}
        out[r.bank_id] = {"source_id": r.source_id, **res}
        print(f"{r.source_id:<16} {res['verdict']:<30} {res.get('tail','')[:60]}", flush=True)
        log.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    n_inv = sum(1 for v in out.values() if v.get("verdict") == "INVALID_buggy_also_passes")
    n_ok = sum(1 for v in out.values() if v.get("verdict") == "ok_buggy_fails")
    print(f"\n筛查累计: 无效 {n_inv} | 有效 {n_ok} | 其他 {len(out)-n_inv-n_ok}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
