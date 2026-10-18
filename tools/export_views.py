"""视图导出：按条件切出选题子集（bank/views/*.json），pull_tasks.py 消费。

2026-10-09 起「去 Docker 化」：
  主力视图只含本机可直接跑的题（env_type: none/conda/venv）
  依赖 Docker 的 12 万题归入 dormant_docker / anchor_dormant（休眠，不参与日常选题）

内置视图:
  runnable_all    全部本机可跑题
  fresh_all       其中晚于全部模型 cutoff 的新鲜题
  lcb_easy/medium/hard  LiveCodeBench 分难度
  perf            SWE-Perf（conda）
  java_classic    Defects4J（便携 JDK）
  python_classic  BugsInPy（venv）
  dormant_docker  休眠：全部 Docker 依赖题
  anchor_dormant  休眠：新鲜锚题（含镜像体积过滤，复活时用）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from env import MODEL_CUTOFFS, VIEWS  # noqa: E402

LATEST_CUTOFF = max(MODEL_CUTOFFS.values())


def build_views() -> dict[str, list[str]]:
    import pandas as pd
    sys.path.insert(0, str(Path(__file__).parent / "importers"))
    from common import load_index

    df = load_index()
    if df.empty:
        raise SystemExit("索引为空，先跑 build_index.py")

    RUNNABLE = df["env_type"].isin(["none", "conda", "venv"])
    DORMANT = df["env_type"].isin(["docker_image", "docker_build"])
    has_date = df["created_at"].str.len() >= 7
    views: dict[str, list[str]] = {}

    # ---- 主力（本机可直接跑，无需 Docker/WSL）----
    views["runnable_all"] = df[RUNNABLE]["bank_id"].tolist()
    views["fresh_all"] = df[RUNNABLE & has_date
                            & (df["created_at"].str[:7] > LATEST_CUTOFF)]["bank_id"].tolist()
    for level in ("easy", "medium", "hard"):
        views[f"lcb_{level}"] = df[(df["source"] == "livecodebench")
                                   & (df["initial_difficulty"] == level)]["bank_id"].tolist()
    views["perf"] = df[df["source"] == "sweperf"]["bank_id"].tolist()
    views["java_classic"] = df[df["source"] == "defects4j"]["bank_id"].tolist()
    views["python_classic"] = df[df["source"] == "bugsinpy"]["bank_id"].tolist()

    # ---- 对表抽样（Docker 不可用，仅保留索引记录供未来参考）----
    ver = df[df["source"] == "swebench_verified"]
    views["verified_sample"] = ver.sample(min(25, len(ver)), random_state=42)["bank_id"].tolist() if len(ver) else []

    # ---- 休眠（需 Docker 或远程执行机才可复活）----
    views["dormant_docker"] = df[DORMANT]["bank_id"].tolist()
    pool = df[df["source"].isin(["swelive_py", "swelive_multilang", "swelive_windows",
                                 "swerebench_v1", "swerebench_v2"])
              & has_date
              & (df["created_at"].str[:7] > LATEST_CUTOFF) & (df["image_ref"].str.len() > 0)]
    pool = pool[~pool["image_ref"].str.contains(".win.")]
    sizes_file = Path(__file__).parent.parent / "bank" / "image_sizes.json"
    sizes = json.loads(sizes_file.read_text(encoding="utf-8")) if sizes_file.exists() else {}
    small = pool[pool["image_ref"].map(lambda r: sizes.get(r) is not None and sizes[r] <= 2 * 1024**3)]
    if len(small) >= 50:
        pool = small
    views["anchor_dormant"] = pool.sample(min(100, len(pool)), random_state=42)["bank_id"].tolist() if len(pool) else []
    return views


def main() -> int:
    VIEWS.mkdir(parents=True, exist_ok=True)
    views = build_views()
    for name, ids in views.items():
        (VIEWS / f"{name}.json").write_text(json.dumps(ids, ensure_ascii=False, indent=0), encoding="utf-8")
        print(f"{name:<18} {len(ids):>7} 题")
    return 0


if __name__ == "__main__":
    sys.exit(main())
