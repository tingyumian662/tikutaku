"""D4J 金标批量预检：checkout → 金标应用 → score_d4j → 应全 pass。

用法: D:/tikutaku/venv/Scripts/python.exe tools/d4j_gold_audit.py [view_or_ids]
默认 view=d4j_first20。输出 logs/d4j_gold_audit.json
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "tools"))

import scoring  # noqa: E402
from seal_gold import unseal  # noqa: E402


def gold_patch_files(bank_id: str, wd: Path) -> dict[str, str] | None:
    patch = unseal(bank_id)
    if not patch:
        return None
    sections = patch.split("diff --git ")
    kept = ["diff --git " + s for s in sections[1:]
            if ".defects4j.config" not in s.split("\n")[0]]
    if not kept:
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False, newline="") as f:
        f.write("".join(kept))
        tmp = f.name
    try:
        subprocess.run(["git", "checkout", "."], cwd=str(wd), capture_output=True, timeout=120)
        subprocess.run(["git", "clean", "-fdq"], cwd=str(wd), capture_output=True, timeout=120)
        ap = subprocess.run(["git", "apply", "--whitespace=nowarn", "--ignore-whitespace", tmp],
                            cwd=str(wd), capture_output=True, text=True, timeout=120)
        if ap.returncode != 0:
            # 金标与 base 偶有"已并入"偏差：测试文件 hunk 可能早已在 base 里（D4J-000799
            # 实锤）。--reject 落地能落的，只要被拒的全是测试路径就当成功（源码 hunk 才是判分关键）
            subprocess.run(["git", "apply", "--ignore-whitespace", "--reject", tmp],
                           cwd=str(wd), capture_output=True, text=True, timeout=120)
            rejs = list(wd.rglob("*.rej"))
            bad = [p for p in rejs
                   if not scoring._is_test_path(str(p.relative_to(wd))[:-4])]
            for p in rejs + list(wd.rglob("*.orig")):
                p.unlink(missing_ok=True)
            if bad:
                return {"__apply_error__": "; ".join(str(b.relative_to(wd)) for b in bad)[:300]}
        gl = subprocess.run(["git", "status", "--porcelain"], cwd=str(wd),
                            capture_output=True, text=True, timeout=60).stdout.splitlines()
        out = {}
        for line in gl:
            p = line[3:].strip().strip('"')
            if not p or scoring._is_test_path(p):
                continue
            fp = wd / p
            if fp.exists():
                out[p] = fp.read_text(encoding="latin-1")
        return out or None
    finally:
        Path(tmp).unlink(missing_ok=True)


def main() -> int:
    import pandas as pd

    spec = sys.argv[1] if len(sys.argv) > 1 else "d4j_first20"
    if spec.startswith("D4J-"):
        ids = spec.split(",")
    else:
        ids = json.loads((ROOT / "bank" / "views" / f"{spec}.json").read_text(encoding="utf-8"))
    df = pd.read_parquet(ROOT / "bank" / "index.parquet")
    rows = df[df.bank_id.isin(ids)]
    results = {}
    log = ROOT / "logs" / "d4j_gold_audit.json"
    # 合并历史记录：老文件只有 first20 时代的样本，整写会丢
    merged = json.loads(log.read_text(encoding="utf-8")) if log.exists() else {}
    t0 = time.time()
    for i, (_, r) in enumerate(rows.iterrows(), 1):
        bid = r.bank_id
        pid, _, bug = r.source_id.rpartition("-")
        f2p = json.loads(r.fail_to_pass)
        t1 = time.time()
        try:
            wd = scoring.d4j_checkout(pid, bug, bid)
            if wd is None:
                results[bid] = {"verdict": "checkout_fail", "source_id": r.source_id}
            else:
                pf = gold_patch_files(bid, wd)
                if pf is None:
                    results[bid] = {"verdict": "gold_empty_or_apply_fail", "source_id": r.source_id}
                elif "__apply_error__" in pf:
                    results[bid] = {"verdict": "gold_apply_fail", "detail": pf["__apply_error__"], "source_id": r.source_id}
                else:
                    sc = scoring.score_d4j(wd, pf, f2p, timeout_sec=1200, allow_new_files=True)
                    results[bid] = {"source_id": r.source_id, **sc}
        except Exception as e:  # noqa: BLE001
            results[bid] = {"verdict": f"error:{type(e).__name__}", "detail": str(e)[:200],
                            "source_id": r.source_id}
        print(f"[{i}/{len(rows)}] {r.source_id:<14} {results[bid].get('verdict', '?'):<12} "
              f"{results[bid].get('sec', round(time.time() - t1, 1))}s", flush=True)
        log.write_text(json.dumps({**merged, **results}, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = sum(1 for v in results.values() if v.get("verdict") == "pass")
    print(f"\n金标预检: {ok}/{len(results)} pass | 总耗时 {round(time.time() - t0)}s | 报告 {log}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
