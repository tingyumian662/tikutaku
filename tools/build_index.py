"""汇总所有导入器，构建/更新 bank/index.parquet + bank/stats.md。

用法: uv run python tools/build_index.py [--only 源名子串]
幂等：全量重建（seals 是加密追加的，重建会重新封存——注意 seal 会被覆盖为相同内容，无害）。
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "importers"))

from schema import COLUMNS, DTYPES  # noqa: E402


def write_origins(df: pd.DataFrame) -> None:
    """把每题 _origin 写入 bank/tasks/<bank_id>/origin.json，并从索引剔除该列。"""
    import json
    from env import TASKS
    for _, row in df.iterrows():
        if "_origin" not in row or row["_origin"] is None:
            continue
        d = TASKS / row["bank_id"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "origin.json").write_text(
            json.dumps(row["_origin"], ensure_ascii=False, indent=1), encoding="utf-8")
    if "_origin" in df.columns:
        df.drop(columns=["_origin"], inplace=True)


def collect(only: str | None) -> pd.DataFrame:
    import import_defects4j
    import import_others
    import import_repos
    import import_swebench
    import import_tb2

    all_rows: list[dict] = []
    for result in (import_swebench.run_imports(only),
                   import_others.run_imports(only),
                   import_tb2.run_imports(only),
                   import_defects4j.run_imports(only),
                   import_repos.run_imports(only)):
        for rows in result.values():
            all_rows.extend(rows)
    if not all_rows:
        return pd.DataFrame(columns=list(COLUMNS))
    df = pd.DataFrame(all_rows)
    write_origins(df)
    # 只保留 schema 列 + dtype 规整
    df = df[list(COLUMNS)]
    for col, dtype in DTYPES.items():
        df[col] = df[col].astype(dtype)
    return df.sort_values("bank_id").reset_index(drop=True)


def dedup(df: pd.DataFrame) -> pd.DataFrame:
    """跨源去重：同 repo+source_id 视为重复，保留优先级高的源（verified > live > rebench > 其他）。"""
    prio = {"swebench_verified": 0, "swelive_py": 1, "swerebench_v2": 2, "swerebench_v1": 3}
    df["_p"] = df["source"].map(prio).fillna(9)
    before = len(df)
    df = (df.sort_values(["repo", "source_id", "_p"])
            .drop_duplicates(subset=["repo", "source_id"], keep="first")
            .drop(columns="_p"))
    print(f"去重: {before} -> {len(df)}")
    return df


def write_stats(df: pd.DataFrame) -> None:
    from env import BANK
    buf = io.StringIO()
    buf.write("# 题库统计\n\n")
    buf.write(f"总题量: **{len(df)}**\n\n")
    buf.write("## 按源\n\n")
    buf.write(df.groupby("source").size().sort_values(ascending=False).to_markdown() + "\n\n")
    buf.write("## 按类型\n\n")
    buf.write(df.groupby("task_type").size().sort_values(ascending=False).to_markdown() + "\n\n")
    buf.write("## 按语言\n\n")
    buf.write(df.groupby("language").size().sort_values(ascending=False).head(20).to_markdown() + "\n\n")
    yr = df[df["created_at"].str.len() >= 4].copy()
    if len(yr):
        yr["年"] = yr["created_at"].str[:4]
        buf.write("## 按题目年份（vintage，防污染锚点）\n\n")
        buf.write(yr.groupby("年").size().to_markdown() + "\n")
    (BANK / "stats.md").write_text(buf.getvalue(), encoding="utf-8")


def main() -> int:
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    df = collect(only)
    if df.empty:
        print("没有可导入的源（下载未完成？）")
        return 1
    df = dedup(df)
    from common import save_index
    save_index(df)
    write_stats(df)
    print(df["source"].value_counts().to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
