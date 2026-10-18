"""Defects4J 金标重封 v2（tag-diff 法）+ 全量可解性体检。

对 804 道已回填题逐题：
  checkout -v {bid}b（ASCII 工作区）→ git diff BUGGY_VERSION..FIXED_VERSION → 重新封存
  成功 = notes: java_verified（含 checkout 验证）；失败保留原因。
用法: bash -c 'source tools/d4j_env.sh && uv run python -u tools/enrich_defects4j_v2.py'
幂等：notes==java_verified 的跳过。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "importers"))

from env import REPOS  # noqa: E402
from seal_gold import seal  # noqa: E402

D4J = REPOS / "defects4j"
D4J_BIN = D4J / "framework" / "bin" / "defects4j"
WORK = Path("E:/AI/d4j_work/batch")


def sh(cmd: list[str], cwd=None, timeout=600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=cwd, timeout=timeout)


def main() -> int:
    import json

    import pandas as pd
    from common import load_index, save_index

    df = load_index()
    todo = df[(df["source"] == "defects4j") & (df["notes"] == "java_local")]
    print(f"待重封+体检: {len(todo)} 题")
    WORK.mkdir(parents=True, exist_ok=True)

    ok = fail = 0
    failures = []
    t0 = time.time()
    for i, (idx, row) in enumerate(todo.iterrows(), 1):
        pid, bug_id = row["source_id"].rsplit("-", 1)
        wd = WORK / "cur"
        subprocess.run(["rm", "-rf", str(wd)], capture_output=True)
        cp = sh(["perl", str(D4J_BIN), "checkout", "-p", pid, "-v", f"{bug_id}b", "-w", str(wd)],
                timeout=300)
        if cp.returncode != 0:
            fail += 1; failures.append((row["source_id"], "checkout失败"))
            df.loc[idx, "notes"] = "java_checkout_fail"
            continue
        tag_b, tag_f = f"D4J_{pid}_{bug_id}_BUGGY_VERSION", f"D4J_{pid}_{bug_id}_FIXED_VERSION"
        diff = sh(["git", "diff", tag_b, tag_f], cwd=wd)
        if cp.returncode != 0 or not diff.stdout.strip():
            fail += 1; failures.append((row["source_id"], "tag diff为空"))
            df.loc[idx, "notes"] = "java_diff_fail"
            continue
        seal(row["bank_id"], diff.stdout)
        df.loc[idx, "notes"] = "java_verified"
        ok += 1
        if i % 25 == 0:
            save_index(df)
            el = time.time() - t0
            print(f"  [{i}/{len(todo)}] ok={ok} fail={fail} 平均 {el/i:.1f}s/题 预计剩余 {(len(todo)-i)*el/i/60:.0f} 分钟")
    save_index(df)
    print(f"完成: 成功 {ok} / 失败 {fail}, 总用时 {(time.time()-t0)/60:.0f} 分钟")
    for sid, why in failures[:15]:
        print(f"  [失败] {sid}: {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
