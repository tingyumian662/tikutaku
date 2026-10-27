"""M3 双向鉴别：干净基线全过（父题级缓存）+ 注入版跑同测试必有失败 → F2P 封存。

- 鉴别语义（2026-10-19 设计定稿）：缺陷版部分测试失败（1 ≤ 失败 ≤ max(3, 总数/3)，
  保证"合理但错误"非全面崩坏）+ 基线版全过；失败节点即该合成题的 fail_to_pass。
- 测试跑在父题 fixed 快照（= 合成题干净基线）上，注入后 finally 还原（2026-10-16
  泄题事故教训：快照必须保持干净）。
- 顺序执行：同一父题的多个候选共享快照，并行会互相踩。
- WSL pytest 环境块与 score_bip 完全一致（PYTHONPATH/gcc-10/懒编译）。

用法: D:/tikutaku/venv/Scripts/python.exe tools/smith_verify.py [--limit N] [--timeout 900]
输入: logs/smith/injected.jsonl（status=ok）→ 输出: logs/smith/verified.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smith_common as sc  # noqa: E402

import scoring  # noqa: E402


def run_pytest(workdir: Path, venv: str, targets: list[str], timeout: float) -> dict:
    """WSL pytest（score_bip 同款环境块），枚举 FAILED/ERROR 节点与汇总数。"""
    wd = scoring._wsl_path(workdir)
    nodes = " ".join(f"'{t}'" for t in targets)
    script = f"""#!/bin/bash
cd {wd} && export PATH="{venv}/bin:$PATH" && \
export PYTHONPATH="$PWD:$PWD/lib:$PWD/src:$PYTHONPATH"; \
export MPLBACKEND=Agg; export PYTHONWARNINGS=ignore::DeprecationWarning; \
command -v gcc-10 >/dev/null && export CC=gcc-10; \
if [ -f setup.py ] && [ ! -f .tikutaku_built ] && [ -z "$TIKU_SKIP_LAZY_BUILD" ]; then \
python setup.py build_ext --inplace -j 0 >/dev/null 2>&1 && touch .tikutaku_built; fi
O=/tmp/smith_out_$$.txt
{venv}/bin/python -m pytest -q --tb=no -p no:cacheprovider {nodes} > $O 2>&1
rc=$?
cat $O | tail -40
rm -f $O
exit $rc
"""
    sf = scoring.BIP_TASKS_ROOT / f"_smith_{uuid.uuid4().hex[:8]}.sh"
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(script, encoding="utf-8", newline="\n")
    try:
        cp = scoring._wsl_run(f"bash {scoring._wsl_path(sf)}", timeout)
    except Exception as e:  # noqa: BLE001
        return {"timeout_or_error": f"{type(e).__name__}:{str(e)[:80]}"}
    finally:
        sf.unlink(missing_ok=True)
    body = (cp.stdout or "") + (cp.stderr or "")
    failed, errored = [], []
    for line in body.splitlines():
        if line.startswith("FAILED "):
            failed.append(line.split()[1])
        elif line.startswith("ERROR "):
            errored.append(line.split()[1])
    m_f = re.search(r"(\d+) failed", body)
    m_p = re.search(r"(\d+) passed", body)
    n_failed = int(m_f.group(1)) if m_f else len(failed)
    n_passed = int(m_p.group(1)) if m_p else -1
    return {"failed": failed, "errored": errored, "n_failed": n_failed, "n_passed": n_passed,
            "collect_error": "ERROR collecting" in body or "ImportError" in body or "ModuleNotFoundError" in body,
            "rc": cp.returncode, "tail": body[-200:].replace("\n", " ")}


def baseline(parent_bid: str, repo: str, bug_dir: str, workdir: Path, cache: dict,
             timeout: float, test_files: list[str]) -> tuple[dict | None, str]:
    """父题级基线（缓存）：干净快照跑整测试文件，记失败节点集（环境性失败）。

    整文件失败 ≠ 父题不可用（真实 BIP 判分只跑 fail_to_pass 子集）；逐节点语义：
    F2P = 注入版失败 − 基线版失败。
    """
    if parent_bid in cache:
        return cache[parent_bid], cache[parent_bid].get("note", "")
    venv = scoring.bip_venv(repo, Path(bug_dir) / "requirements.txt" if bug_dir else None,
                            bank_id=parent_bid)
    if venv is None:
        cache[parent_bid] = {"failed_clean": None, "note": "venv_fail"}
        return None, "venv_fail"
    r = run_pytest(workdir, venv, test_files, timeout)
    if "timeout_or_error" in r:
        cache[parent_bid] = {"failed_clean": None, "note": f"baseline:{r['timeout_or_error']}"}
        return None, cache[parent_bid]["note"]
    n_total = r["n_passed"] + r["n_failed"] if r["n_passed"] >= 0 else len(r["failed"])
    cache[parent_bid] = {"venv": venv, "failed_clean": sorted(set(r["failed"]) | set(r["errored"])),
                         "n_total": n_total, "note": ""}
    return cache[parent_bid], ""


def main() -> int:
    ap = argparse.ArgumentParser(description="双向鉴别封 F2P（可续跑）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=900)
    args = ap.parse_args()

    injected = [r for r in sc.read_jsonl(sc.INJECTED) if r.get("status") == "ok"]
    done = {r["cand_id"] for r in sc.read_jsonl(sc.VERIFIED)}
    todo = [r for r in injected if r["cand_id"] not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f"注入成功 {len(injected)} | 已鉴别 {len(done)} | 本轮 {len(todo)}")
    if not todo:
        return 0

    cache = json.loads(sc.BASELINE_CACHE.read_text(encoding="utf-8")) if sc.BASELINE_CACHE.exists() else {}
    stats: dict[str, int] = {}
    for r in todo:
        t0 = time.time()
        row = {k: r[k] for k in ("cand_id", "parent_bid", "repo", "repo_url", "base_commit",
                                 "bug_dir", "test_files", "target_path", "statement",
                                 "buggy_content", "model", "gen_at")}
        snap = sc.fixed_snapshot(r["parent_bid"], r["repo_url"], r["base_commit"])
        if snap is None:
            row.update(status="reject", reason="snapshot_missing")
        else:
            base, note = baseline(r["parent_bid"], r["repo"], r["bug_dir"], snap, cache,
                                  args.timeout, r["test_files"])
            if base is None:
                row.update(status="reject", reason=f"baseline:{note[:80]}")
            else:
                venv = base["venv"]
                failed_clean = set(base["failed_clean"])
                n_total = base.get("n_total", 0)
                fp = snap / r["target_path"]
                orig = fp.read_text(encoding="latin-1")
                try:
                    fp.write_text(r["buggy_content"], encoding="latin-1", newline="\n")
                    res = run_pytest(snap, venv, r["test_files"], args.timeout)
                finally:
                    fp.write_text(orig, encoding="latin-1", newline="\n")  # 快照必须还原干净
                if "timeout_or_error" in res:
                    row.update(status="reject", reason=f"injected:{res['timeout_or_error']}")
                elif res["collect_error"] and not res["failed"]:
                    row.update(status="reject", reason="injected:collect_error")
                else:
                    # 逐节点双向鉴别：注入新增失败 = 注入版失败 − 基线版失败
                    f2p = sorted(set(res["failed"]) - failed_clean)
                    cap = max(3, n_total // 3) if n_total > 0 else 1
                    if not f2p:
                        row.update(status="reject", reason="no_discrimination", n_total=n_total)
                    elif len(f2p) > cap:
                        row.update(status="reject", reason=f"too_gross:{len(f2p)}/{n_total}")
                    else:
                        row.update(status="ok", f2p=f2p, n_failed=len(f2p), n_total=n_total)
        row["sec"] = round(time.time() - t0, 1)
        stats[row["status"]] = stats.get(row["status"], 0) + 1
        sc.append_jsonl(sc.VERIFIED, row)
        sc.BASELINE_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        info = row.get("reason") or f"{row.get('n_failed')}F/{row.get('n_total')}"
        print(f"  {row['cand_id']} {row['repo']:<12} {row['status']:<6} {info[:56]} {row['sec']}s",
              flush=True)
    print(f"\n本轮: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
