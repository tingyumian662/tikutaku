"""Phase 1: 从 HF 镜像站全量下载 14 源数据集到 sources/datasets/。

用法: uv run python tools/download_datasets.py [--only 名称子串]
幂等：已完成的源跳过（标记文件 .done）。
"""
import sys
import time
import traceback

from huggingface_hub import snapshot_download

from env import DATASETS, LOGS

# (目录名, HF repo id, 说明, 预计体积MB)
MANIFEST = [
    ("swe-bench_verified", "princeton-nlp/SWE-bench_Verified", "SWE-bench Verified 500 题（对表抽样用）", 3),
    ("swe-bench_full", "princeton-nlp/SWE-bench", "SWE-bench 全量 2294", 40),
    ("swe-bench_lite", "princeton-nlp/SWE-bench_Lite", "SWE-bench Lite 300", 5),
    ("swe-bench_multimodal", "SWE-bench/SWE-bench_Multimodal", "多模态 517", 100),
    ("swe-bench-live", "SWE-bench-Live/SWE-bench-Live", "SWE-bench-Live Python 1888+", 190),
    ("swe-bench-live_multilang", "SWE-bench-Live/MultiLang", "SWE-bench-Live 多语言 1077", 411),
    ("swe-bench-live_windows", "SWE-bench-Live/Windows", "SWE-bench-Live Windows 66", 20),
    ("swe-rebench_v1", "nebius/SWE-rebench", "SWE-rebench V1 21336", 245),
    ("swe-rebench_v2", "nebius/SWE-rebench-V2", "SWE-rebench V2 32079", 2260),
    ("multi-swe-bench", "ByteDance-Seed/Multi-SWE-bench", "Multi-SWE-bench 1632", 30),
    ("multi-swe-bench_mini", "ByteDance-Seed/Multi-SWE-bench_mini", "mini 400", 10),
    ("multi-swe-bench_flash", "ByteDance-Seed/Multi-SWE-bench-flash", "flash 300", 8),
    ("swe-smith", "SWE-bench/SWE-smith", "SWE-smith 合成 59136", 277),
    ("r2e-gym_lite", "R2E-Gym/R2E-Gym-Lite", "R2E-Gym Lite 4578", 2200),
    ("terminal-bench_2.0", "harborframework/terminal-bench-2.0", "Terminal-Bench 2.0 89 题", 67),
    ("terminal-bench_2.1", "harborframework/terminal-bench-2.1", "Terminal-Bench 2.1 89 题", 49),
    ("livecodebench_lite", "livecodebench/code_generation_lite", "LiveCodeBench 1055（开放版）", 15),
    ("swe-perf", "SWE-Perf/SWE-Perf", "SWE-Perf 性能 140", 2),
]


def download_one(name: str, repo_id: str) -> bool:
    dest = DATASETS / name
    done_marker = dest / ".done"
    # 部分完成的（有 PARTIAL.txt）自动重下，snapshot_download 会续传已有文件
    if (dest / "PARTIAL.txt").exists() and done_marker.exists():
        done_marker.unlink()
        (dest / "PARTIAL.txt").unlink()
        print(f"[partial] {name} 检测到 PARTIAL 标记，重新补全")
    if done_marker.exists():
        print(f"[skip] {name} 已完成")
        return True
    dest.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, 4):
        try:
            t0 = time.time()
            snapshot_download(
                repo_id=repo_id,
                repo_type="dataset",
                local_dir=str(dest),
                max_workers=4,
            )
            done_marker.touch()
            print(f"[done] {name} ({repo_id}) 用时 {time.time()-t0:.0f}s")
            return True
        except Exception as e:
            print(f"[retry {attempt}/3] {name}: {type(e).__name__}: {e}")
            time.sleep(10 * attempt)
    print(f"[FAIL] {name} 三次失败，跳过")
    return False


def main() -> int:
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    LOGS.mkdir(parents=True, exist_ok=True)
    fails = []
    for name, repo_id, desc, _mb in MANIFEST:
        if only and only not in name:
            continue
        print(f"==== {name} | {repo_id} | {desc} ====")
        if not download_one(name, repo_id):
            fails.append(name)
    print("\n==== 总结 ====")
    print(f"失败源: {fails if fails else '无'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
