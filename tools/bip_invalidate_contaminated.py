"""作废 BIP 污染读判定：audit 写金标不恢复 → 模型 prompt 见答案（2026-10-16 事故）。

污染集 = logs/bip_contamination_20261016.json 的 dirty 列表（读时刻晚于该题金标写入时刻）。
处理：results.jsonl 中这些题的有效判定改 error:contaminated_prompt（原值存 contaminated_verdict
留痕），--skip-done 不认 error → 自动重测。生成运行在污染窗口内的记录才改。
"""
from __future__ import annotations

import json
import glob
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

contam = json.load(open(ROOT / "logs" / "bip_contamination_20261016.json", encoding="utf-8"))
dirty = {b: (run, v) for b, run, v in contam["dirty"]}
DIRTY_RUNS = {"20261015-183754", "20261015-210900", "20261016-072845"}

n_files = n_recs = 0
for f in sorted(glob.glob(str(ROOT / "results" / "run-*" / "results.jsonl"))):
    run = Path(f).parent.name.replace("run-", "")
    if run not in DIRTY_RUNS:
        continue
    lines, touched = [], False
    for line in open(f, encoding="utf-8"):
        try:
            rec = json.loads(line)
        except Exception:
            lines.append(line)
            continue
        v = (rec.get("score") or {}).get("verdict", "")
        if rec.get("bank_id") in dirty and v and not str(v).startswith("error") \
                and v != "empty_reply":
            rec["score"]["verdict"] = "error:contaminated_prompt"
            rec["score"]["contaminated_verdict"] = v
            rec["score"]["note"] = "prompt-read-after-gold-audit-write (2026-10-16)"
            touched = True
            n_recs += 1
        lines.append(json.dumps(rec, ensure_ascii=False) + "\n")
    if touched:
        Path(f).write_text("".join(lines), encoding="utf-8")
        n_files += 1
        print(f"{run}: 已作废记录写入")

print(f"\n完成：{n_recs} 条判定作废（跨 {n_files} 个 run 文件）；期望 58")
