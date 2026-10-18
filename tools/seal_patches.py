"""封存 tmp/patches/*.patch → bank/seals/，并回写索引 notes=java_verified。纯原生 Python，无 MSYS 依赖。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "importers"))

from seal_gold import seal  # noqa: E402


def main() -> int:
    from common import load_index, save_index
    from env import ROOT

    patch_dir = ROOT / "tmp" / "patches"
    files = sorted(patch_dir.glob("*.patch"))
    print(f"待封存 {len(files)} 个金标 patch")

    df = load_index()
    idx = {b: i for b, i in zip(df["bank_id"], df.index)}
    ok = miss = 0
    for f in files:
        bid = f.stem
        if bid not in idx:
            miss += 1
            continue
        seal(bid, f.read_text(encoding="utf-8", errors="replace"))
        df.loc[idx[bid], "notes"] = "java_verified"
        ok += 1
    save_index(df)

    d = df[df["source"] == "defects4j"]
    print(f"封存 {ok} 个（索引未命中 {miss}）")
    print("notes 分布:", dict(d["notes"].value_counts()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
