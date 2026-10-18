# -*- coding: utf-8 -*-
"""BIP 其余 repo 标定接力：重筛 → 建视图 → 完整金标重封 → 金标审计。

前置：8-repo 筛查驱动已完成。产物：bank/views/bip_rest.json（有效题）+ 审计结果打印。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = os.environ.get("TIKUTAKU_PYTHON", "D:/tikutaku/venv/Scripts/python.exe")


def run(cmd: list[str], timeout: int = 14400) -> int:
    print(f"\n$ {' '.join(cmd)}", flush=True)
    try:
        return subprocess.run(cmd, cwd=str(ROOT), timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        print(f"[warn] 超时 {timeout}s，继续下一阶段（幂等可重跑）", flush=True)
        return 124


def main() -> int:
    # 1) 重筛：tornado（旧判定逻辑误分类）+ ansible（符号链接修复）+ 其余 env_error 仓库（CC=gcc-10 修复）
    import pandas as pd
    screen0 = json.loads((ROOT / "logs" / "bip_screen.json").read_text(encoding="utf-8"))
    df0 = pd.read_parquet(ROOT / "bank" / "index.parquet")
    bid2repo = dict(zip(df0[df0.source == "bugsinpy"].bank_id, df0[df0.source == "bugsinpy"].repo))
    rerun = {"tornado", "ansible", "scrapy"}
    for b, v in screen0.items():
        if v.get("verdict") == "env_error":
            rerun.add(bid2repo.get(b, "?"))
    for repo in sorted(rerun):
        rc = run([PY, "-u", "tools/bip_screen_invalid.py", repo])
        if rc != 0:
            print(f"[warn] {repo} 重筛退出码 {rc}")

    # 2) 有效视图：全部 ok_buggy_fails 且不在 pandas 已标定视图里
    df = pd.read_parquet(ROOT / "bank" / "index.parquet")
    screen = json.loads((ROOT / "logs" / "bip_screen.json").read_text(encoding="utf-8"))
    pandas_ids = set(json.loads((ROOT / "bank" / "views" / "bip_pandas.json").read_text(encoding="utf-8")))
    valid = [b for b, v in screen.items()
             if v.get("verdict") == "ok_buggy_fails" and b not in pandas_ids]
    valid = sorted(set(valid) & set(df[df.source == "bugsinpy"].bank_id))
    (ROOT / "bank" / "views" / "bip_rest.json").write_text(
        json.dumps(valid, indent=0), encoding="utf-8")
    from collections import Counter
    by_repo = Counter(df.set_index("bank_id").loc[b, "repo"] for b in valid)
    print(f"\nbip_rest 视图: {len(valid)} 题 | {dict(by_repo)}")

    # 3) Stage B：完整金标重封（工作区作 buggy 参照，fixed 现场拉）
    rc = run([PY, "-u", "tools/bip_reseal_complete.py", ",".join(valid)], timeout=14400)
    if rc != 0:
        print(f"[warn] 重封退出码 {rc}")

    # 4) Stage C：金标审计（捕获输出；上轮已 pass 的增量跳过；pass 集回流过滤最终视图）
    import re
    audit_log = ROOT / "logs" / "bip_rest_audit.txt"
    sid2bank = dict(zip(df.source_id, df.bank_id))
    prev_pass = set()
    if audit_log.exists():
        for ln in audit_log.read_text(encoding="utf-8").splitlines():
            m = re.match(r"(\S+)\s+pass\s", ln)
            if m and m.group(1) in sid2bank:
                prev_pass.add(sid2bank[m.group(1)])
    to_audit = [b for b in valid if b not in prev_pass]
    print(f"\n审计: 上轮已 pass {len(prev_pass)}，本轮需审 {len(to_audit)}")
    out_lines = [f"{df.set_index('bank_id').loc[b, 'source_id']:<16} pass         (cached)"
                 for b in valid if b in prev_pass]
    try:
        cp = subprocess.run([PY, "-u", "tools/bip_gold_audit.py", ",".join(to_audit)],
                            cwd=str(ROOT), capture_output=True, text=True, timeout=28800)
        out_lines += (cp.stdout or "").splitlines()
        print(cp.stdout[-2000:] if cp.stdout else "(审计无输出)", flush=True)
    except subprocess.TimeoutExpired as e:
        out_lines += ((e.stdout or "").splitlines() if isinstance(e.stdout, str) else [])
        print("[warn] 审计超时")
    audit_log.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    passed_ids = {sid2bank[m.group(1)] for m in
                  (re.match(r"(\S+)\s+pass\s", ln) for ln in out_lines)
                  if m and m.group(1) in sid2bank}
    final = [b for b in valid if b in passed_ids]
    (ROOT / "bank" / "views" / "bip_rest.json").write_text(
        json.dumps(final, indent=0), encoding="utf-8")
    print(f"\n审计累计 pass {len(passed_ids)}/{len(valid)} → 最终 bip_rest 视图 {len(final)} 题")
    return 0


if __name__ == "__main__":
    sys.exit(main())
