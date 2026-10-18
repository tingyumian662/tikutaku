"""Perf 金标批量预检：物化 → test_patch+金标 → 提取 patch_files → score_perf → 应 pass。

用法: D:/tikutaku/venv/Scripts/python.exe tools/perf_gold_audit.py [repo前缀]
默认 sympy+xarray（环境已验证）；输出 logs/perf_gold_audit.json（增量合并）
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "tools"))

import perf_scoring as ps  # noqa: E402
import scoring  # noqa: E402
from seal_gold import unseal  # noqa: E402


def audit_one(r, blob: dict) -> dict:
    wd = ps.perf_checkout(r.bank_id, r.repo, blob["base_commit"])
    if wd is None:
        return {"verdict": "checkout_fail"}
    ps._apply_patch(wd, blob["test_patch"])
    ps._apply_patch(wd, blob["patch"], tolerate_meta=True)
    pf = {}
    for p in re.findall(r"diff --git a/(\S+) b/\S+", blob["patch"]):
        if not scoring._is_test_path(p) and (wd / p).exists():
            pf[p] = (wd / p).read_text(encoding="latin-1")
    subprocess.run(["git", "checkout", "."], cwd=str(wd), capture_output=True, env=ps._genv())
    ps._apply_patch(wd, blob["test_patch"])
    proj = str(r.repo).split("/")[-1]
    venv = ps.perf_venv(proj, blob.get("version", ""), spec_workdir=wd, spec_repo=str(r.repo))
    if venv is None:
        return {"verdict": "venv_fail"}
    return ps.score_perf(wd, pf, venv, blob["efficiency_test"][0], blob, runs=3, project=proj)


def main() -> int:
    import pandas as pd

    prefix = sys.argv[1] if len(sys.argv) > 1 else "sympy|pydata"
    df = pd.read_parquet(ROOT / "bank" / "index.parquet")
    rows = df[(df.source == "sweperf") & df.source_id.str.contains(prefix, regex=True)]
    log = ROOT / "logs" / "perf_gold_audit.json"
    out = json.loads(log.read_text(encoding="utf-8")) if log.exists() else {}
    for _, r in rows.iterrows():
        if r.bank_id in out:
            continue
        try:
            blob = json.loads(unseal(r.bank_id))
            res = audit_one(r, blob)
        except Exception as e:  # noqa: BLE001
            res = {"verdict": f"error:{type(e).__name__}", "tail": str(e)[:150]}
        out[r.bank_id] = {"source_id": r.source_id, **res}
        print(f"{r.source_id:<28} {res.get('verdict', '?'):<22} "
              f"{str(res.get('speedup', res.get('tail', '')))[:50]}", flush=True)
        log.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = sum(1 for v in out.values() if v.get("verdict") == "pass")
    print(f"\nPerf 金标累计: {ok}/{len(out)} pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
