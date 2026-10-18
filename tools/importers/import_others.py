"""导入器：Multi-SWE-bench 系 / SWE-smith / R2E-Gym / SWE-Perf / LiveCodeBench。

各自 schema 差异较大，逐个映射。
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
from import_swebench import _get, _tests  # noqa: E402


def import_multiswebench(source: str, dirname: str, data_root: Path) -> list[dict]:
    """字段: org/repo/number/title/body/fix_patch/test_patch/f2p_tests/p2p_tests..."""
    df = read_any(data_root / dirname)
    lang_of_repo = {}  # 靠 dataset 里可能的 language 列，缺省 java
    tasks = []
    for i, row in df.iterrows():
        sid = str(_get(row, "instance_id", default=f"{_get(row,'repo')}-{_get(row,'number')}"))
        patch = str(_get(row, "fix_patch", default=""))
        org = str(_get(row, "org", default=""))
        repo = str(_get(row, "repo", default=""))
        t = new_task(
            source=source,
            source_id=sid,
            source_url=f"https://github.com/{org}/{repo}/pull/{_get(row,'number')}" if org else "",
            language=str(_get(row, "language", default="")) or "unknown",
            repo=f"{org}/{repo}" if org else repo,
            task_type="bug_fix",
            problem_statement=(str(_get(row, "title", default="")) + "\n\n" + str(_get(row, "body", default="")))[:8000],
            fail_to_pass=jdump(_tests(row, "f2p_tests", "fixed_tests")),
            pass_to_pass=jdump(_tests(row, "p2p_tests")),
            image_ref=f"mswebench/{repo}:pr-{_get(row,'number')}",
        )
        t["env_type"] = "docker_image"
        t["image_registry"] = "dockerhub:mswebench"
        t["bank_id"] = bank_id(source, i + 1)
        t["gold_seal"] = seal(t["bank_id"], patch)
        t["_origin"] = origin_from_row(row)
        tasks.append(t)
    return tasks


def import_swesmith(source: str, dirname: str, data_root: Path) -> list[dict]:
    """字段: instance_id/patch/FAIL_TO_PASS/PASS_TO_PASS/image_name/repo/problem_statement（合成任务）"""
    df = read_any(data_root / dirname)
    tasks = []
    for i, row in df.iterrows():
        sid = str(_get(row, "instance_id", default=f"row{i}"))
        image = str(_get(row, "image_name", default=""))
        # SWE-smith 镜像 image_name 中 "__" 需替换为 "_1776_"（jyangballin 命名空间兼容）
        image_hub = image.replace("__", "_1776_") if image else ""
        t = new_task(
            source=source,
            source_id=sid,
            language="python",
            repo=str(_get(row, "repo", default="")),
            task_type="bug_fix",
            problem_statement=str(_get(row, "problem_statement", default=""))[:8000],
            fail_to_pass=jdump(_tests(row, "FAIL_TO_PASS")),
            pass_to_pass=jdump(_tests(row, "PASS_TO_PASS")),
            image_ref=image_hub,
            env_notes="合成注入 bug，天然零污染；需 Ubuntu22.04/WSL2",
        )
        t["env_type"] = "docker_image"
        t["image_registry"] = "dockerhub:jyangballin"
        t["created_at"] = ""   # 合成任务无 issue 日期
        t["notes"] = "synthetic"
        t["bank_id"] = bank_id(source, i + 1)
        t["gold_seal"] = seal(t["bank_id"], str(_get(row, "patch", default="")))
        t["_origin"] = origin_from_row(row)
        tasks.append(t)
    return tasks


def import_r2egym(source: str, dirname: str, data_root: Path) -> list[dict]:
    """字段: repo_name/docker_image/commit_hash/.../problem_statement/expected_output_json"""
    df = read_any(data_root / dirname)
    tasks = []
    for i, row in df.iterrows():
        sid = str(_get(row, "commit_hash", default=f"row{i}"))
        t = new_task(
            source=source,
            source_id=sid,
            language="python",
            repo=str(_get(row, "repo_name", default="")),
            task_type="bug_fix",
            problem_statement=str(_get(row, "problem_statement", "prompt", default=""))[:8000],
            verifier_type="endtoend",  # R2E-Gym 用混合验证器（执行结果比对）
            image_ref=str(_get(row, "docker_image", default="")),
        )
        t["env_type"] = "docker_image"
        t["image_registry"] = "dockerhub:namanjain12"
        t["notes"] = "license_unverified_internal_only"
        t["bank_id"] = bank_id(source, i + 1)
        t["gold_seal"] = seal(t["bank_id"], str(_get(row, "parsed_commit_content", default="")))
        t["_origin"] = origin_from_row(row)
        tasks.append(t)
    return tasks


def import_sweperf(source: str, dirname: str, data_root: Path) -> list[dict]:
    """字段: repo/instance_id/patch/test_patch/efficiency_test/problem_statement_oracle/..."""
    df = read_any(data_root / dirname)
    tasks = []
    for i, row in df.iterrows():
        sid = str(_get(row, "instance_id", default=f"row{i}"))
        t = new_task(
            source=source,
            source_id=sid,
            language="python",
            repo=str(_get(row, "repo", default="")),
            task_type="perf",
            problem_statement=str(_get(row, "problem_statement_realistic", "problem_statement_oracle", default=""))[:8000],
            verifier_type="efficiency_test",
            env_type="conda",
            env_notes="SWE-Perf 官方 harness，conda 环境自动创建",
            fail_to_pass=jdump(_tests(row, "efficiency_test")),
        )
        t["bank_id"] = bank_id(source, i + 1)
        t["gold_seal"] = seal(t["bank_id"], str(_get(row, "patch", default="")))
        t["_origin"] = origin_from_row(row)
        tasks.append(t)
    return tasks


def import_livecodebench(source: str, dirname: str, data_root: Path) -> list[dict]:
    """LCB-lite 实测列: question_content/contest_date/difficulty/public_test_cases/private_test_cases/starter_code
    测试格式: JSON 串 {"inputs": [...], "outputs": [...]}（stdin_stdout 型）。"""
    df = read_any(data_root / dirname)
    tasks = []
    seq = 0
    for i, row in df.iterrows():
        pub = _get(row, "public_test_cases", default=None)
        tests = []
        if isinstance(pub, str) and pub.strip().startswith(("[", "{")):
            try:
                d = json.loads(pub)
                items = d if isinstance(d, list) else [d]
                for t in items:
                    if isinstance(t, dict) and "input" in t:
                        tests.append({"input": str(t["input"]),
                                      "expected": str(t.get("output", t.get("expected", "")))})
            except Exception:
                tests = []
        elif isinstance(pub, list):   # 已是 list 形态
            for t in pub:
                if isinstance(t, dict) and "input" in t:
                    tests.append({"input": str(t["input"]), "expected": str(t.get("output", t.get("expected", "")))})
        if not tests:
            continue
        seq += 1
        t = new_task(
            source=source,
            source_id=str(_get(row, "question_id", "question_title", default=f"lcb{i}")),
            language="python",
            repo="",
            task_type="algo",
            problem_statement=str(_get(row, "question_content", default=""))[:16000],
            verifier_type="oj_tests",
            env_type="none",
            env_notes="本地子进程沙箱执行",
            fail_to_pass=jdump(tests),
            pass_to_pass="[]",
            initial_difficulty=str(_get(row, "difficulty", default="unknown")),
        )
        t["created_at"] = str(_get(row, "contest_date", default=""))[:10]
        t["timeout_sec"] = 300
        t["bank_id"] = bank_id(source, seq)
        # OJ 题"金标"没有意义，封存起始代码占位（保持 schema 一致）
        t["gold_seal"] = seal(t["bank_id"], str(_get(row, "starter_code", default="")))
        t["_origin"] = origin_from_row(row)
        tasks.append(t)
    return tasks


IMPORTERS = {
    "multiswebench":       ("multi-swe-bench",        import_multiswebench),
    "multiswebench_mini":  ("multi-swe-bench_mini",   import_multiswebench),
    "multiswebench_flash": ("multi-swe-bench_flash",  import_multiswebench),
    "swesmith":            ("swe-smith",              import_swesmith),
    "r2egym_lite":         ("r2e-gym_lite",           import_r2egym),
    "sweperf":             ("swe-perf",               import_sweperf),
    "livecodebench":       ("livecodebench_lite",     import_livecodebench),
}


def run_imports(only: str | None = None) -> dict[str, list[dict]]:
    from env import DATASETS
    out = {}
    for source, (dirname, fn) in IMPORTERS.items():
        if only and only not in source:
            continue
        if not (DATASETS / dirname / ".done").exists():
            print(f"[skip] {dirname} 未下载完成")
            continue
        try:
            rows = fn(source, dirname, DATASETS)
            out[source] = rows
            print(f"[ok] {source}: {len(rows)} 题")
        except Exception as e:
            print(f"[FAIL] {source}: {type(e).__name__}: {e}")
    return out
