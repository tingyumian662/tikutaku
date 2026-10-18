"""防污染过滤：按模型知识截止日给每题打标，生成 per-model 视图。

规则：题目 created_at（issue/PR 日期）> 模型 cutoff 当月 → 该模型视为"未见过"（ok=1）。
- created_at 为空（合成题/无日期）→ 无法判定（ok=0, reason=no_date）
- 合成注入题（notes=synthetic）→ 天然防污染（ok=1, reason=synthetic）

用法: uv run python tools/cutoff_filter.py   # 重算全表 cutoff_ok_json 列并写回索引
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from env import MODEL_CUTOFFS  # noqa: E402


def judge(task: dict) -> dict[str, dict]:
    """返回 {model: {"ok": bool, "reason": str}}"""
    out = {}
    synthetic = task.get("notes") == "synthetic"
    created = (task.get("created_at") or "").strip()
    if synthetic:
        return {m: {"ok": True, "reason": "synthetic"} for m in MODEL_CUTOFFS}
    if not created or len(created) < 7:
        return {m: {"ok": False, "reason": "no_date"} for m in MODEL_CUTOFFS}
    for m, cutoff in MODEL_CUTOFFS.items():
        # cutoff "2025-06" 与 created_at "2025-07-01" 字符串前缀比较即可（同格式 ISO）
        out[m] = {"ok": created[:7] > cutoff, "reason": "vintage" if created[:7] > cutoff else "before_cutoff"}
    return out


def main() -> int:
    import pandas as pd
    sys.path.insert(0, str(Path(__file__).parent / "importers"))
    from common import load_index, save_index

    df = load_index()
    if df.empty:
        print("索引为空，先跑 build_index.py")
        return 1
    marks = []
    for _, row in df.iterrows():
        marks.append(json.dumps(judge(row.to_dict()), ensure_ascii=False))
    df["cutoff_ok_json"] = marks
    save_index(df)

    # 摘要
    print("\n== 各模型可用题量（防污染视图）==")
    for m in MODEL_CUTOFFS:
        ok = df["cutoff_ok_json"].map(lambda s, m=m: json.loads(s).get(m, {}).get("ok", False))
        print(f"  {m:<20} {int(ok.sum()):>7} / {len(df)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
