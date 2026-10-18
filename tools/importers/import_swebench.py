"""导入器：SWE-bench 家族（Verified/Full/Lite/Multimodal + SWE-bench-Live + SWE-rebench V1/V2）。

这些源共享同构 schema（instance_id/repo/base_commit/patch/test_patch/FAIL_TO_PASS...），
按列名自适应映射。金标 patch 封存到 seals/，索引里只存 seal 引用。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import bank_id, jdump, origin_from_row, read_any  # noqa: E402
from seal_gold import seal  # noqa: E402
from schema import new_task  # noqa: E402


def _get(row, *names, default=""):
    import numpy as np
    for n in names:
        if n in row.index and row[n] is not None:
            v = row[n]
            if isinstance(v, np.ndarray):
                v = v.tolist()
            if isinstance(v, (list, dict)):
                return v
            if isinstance(v, np.generic):
                return v.item()
            return v
    return default


def _tests(row, *names) -> list:
    """FAIL_TO_PASS 等字段可能是 JSON 字符串 / list / dict（Multi-SWE-bench: {测试名: 状态}）。"""
    v = _get(row, *names, default="")
    if isinstance(v, dict):
        return [str(k) for k, s in v.items()
                if not (isinstance(s, dict) and s.get("fix") == "FAIL")]
    if isinstance(v, str):
        v = v.strip()
        if v.startswith("["):
            try:
                return json.loads(v)
            except Exception:
                return []
        return [t for t in v.split(",") if t] if v else []
    if isinstance(v, list):
        return list(v)
    return []


def import_one(source: str, dirname: str, data_root: Path,
               exclude_splits: tuple = ()) -> list[dict]:
    df = read_any(data_root / dirname)
    if "_src_file" in df.columns and exclude_splits:
        n0 = len(df)
        df = df[~df["_src_file"].str.startswith(exclude_splits)].reset_index(drop=True)
        print(f"  排除 split {exclude_splits}: {n0} -> {len(df)}")
    tasks = []
    for i, row in df.iterrows():
        sid = str(_get(row, "instance_id", "id", default=f"row{i}"))
        patch = str(_get(row, "patch", "fix_patch", "gold_patch", default=""))
        image_ref = str(_get(row, "docker_image", "image_name", default=""))
        lang = str(_get(row, "language", default=""))
        if not lang:
            lang = "python"
        t = new_task(
            source=source,
            source_id=sid,
            source_url=str(_get(row, "commit_url", "issue_url", default="")),
            created_at=str(_get(row, "created_at", default=""))[:10],
            language=lang,
            repo=str(_get(row, "repo", "repo_name", default="")),
            task_type="bug_fix",
            problem_statement=str(_get(row, "problem_statement", "pr_description", "title", default=""))[:8000],
            fail_to_pass=jdump(_tests(row, "FAIL_TO_PASS", "f2p_tests", "fixed_tests")),
            pass_to_pass=jdump(_tests(row, "PASS_TO_PASS", "p2p_tests")),
            image_ref=image_ref,
            verifier_type="f2p_p2p_tests",
        )
        # SWE-bench-Live 自带难度三元组（改动文件数/hunk 数/行数）
        d = _get(row, "difficulty_files", default="")
        if d != "":
            t["initial_difficulty"] = f"files={d}"
        # SWE-bench-Live Python 版无镜像列，按官方约定合成引用（MultiLang 实证该命名空间）
        if source.startswith("swelive") and not t["image_ref"]:
            t["image_ref"] = f"starryzhang/sweb.eval.x86_64.{sid}"
        if image_ref:
            t["env_type"] = "docker_image"
            t["image_registry"] = "dockerhub"
        else:
            t["env_type"] = "docker_build"
            t["env_notes"] = "swebench harness 本地构建"
        # 金标封存（索引不含明文 patch）
        t["bank_id"] = bank_id(source, i + 1)
        t["gold_seal"] = seal(t["bank_id"], patch)
        t["_origin"] = origin_from_row(row)
        tasks.append(t)
    return tasks


PLAN = [
    ("swebench_verified",   "swe-bench_verified",     ()),
    ("swebench_full",       "swe-bench_full",         ("train",)),   # train=19k 训练噪声，不入库
    ("swebench_lite",       "swe-bench_lite",         ()),
    ("swebench_multimodal", "swe-bench_multimodal",   ()),
    ("swelive_py",          "swe-bench-live",         ()),
    ("swelive_multilang",   "swe-bench-live_multilang", ()),
    ("swelive_windows",     "swe-bench-live_windows", ()),
    ("swerebench_v1",       "swe-rebench_v1",         ()),
    ("swerebench_v2",       "swe-rebench_v2",         ()),
]


def run_imports(only: str | None = None) -> dict[str, list[dict]]:
    from env import DATASETS
    out = {}
    for source, dirname, excl in PLAN:
        if only and only not in source:
            continue
        if not (DATASETS / dirname / ".done").exists():
            print(f"[skip] {dirname} 未下载完成")
            continue
        try:
            rows = import_one(source, dirname, DATASETS, exclude_splits=excl)
            out[source] = rows
            print(f"[ok] {source}: {len(rows)} 题")
        except Exception as e:
            print(f"[FAIL] {source}: {type(e).__name__}: {e}")
    return out
