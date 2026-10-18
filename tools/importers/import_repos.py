"""导入器（仓库型）：Terminal-Bench-1 / CVE-Bench / BugsInPy / SWE-Lancer。

数据直接从 sources/repos/ 的克隆仓库解析，不依赖 HF。
Defects4J 需要 init.sh 下载 git bundle，留 Phase 2。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import bank_id, jdump  # noqa: E402
from seal_gold import seal  # noqa: E402
from schema import new_task  # noqa: E402


def import_terminalbench1(source: str, repos: Path) -> list[dict]:
    """每题: original-tasks/<name>/{task.yaml, Dockerfile, docker-compose.yaml, run-tests.sh, solution.sh}"""
    tasks_dir = repos / "terminal-bench-1" / "original-tasks"
    tasks = []
    for i, tdir in enumerate(sorted(p for p in tasks_dir.iterdir() if p.is_dir()), 1):
        meta_file = tdir / "task.yaml"
        if not meta_file.exists():
            continue
        try:
            meta = yaml.safe_load(meta_file.read_text(encoding="utf-8")) or {}
        except Exception:
            meta = {}
        instruction = (tdir / "instruction.md")
        ps = instruction.read_text(encoding="utf-8", errors="replace")[:8000] if instruction.exists() else str(meta)
        solution = tdir / "solution.sh"
        t = new_task(
            source=source,
            source_id=tdir.name,
            language="shell",
            repo="terminal-bench-1",
            task_type="terminal",
            problem_statement=ps,
            verifier_type="reward_script",   # run-tests.sh 判定
            env_type="docker_build",
            image_ref="",
            env_notes=f"本地构建: {tdir.name}/Dockerfile；run-tests.sh 判分；测试材料加密保护",
            initial_difficulty=str(meta.get("difficulty", "unknown")),
        )
        t["bank_id"] = bank_id(source, i)
        t["gold_seal"] = seal(t["bank_id"], solution.read_text(encoding="utf-8", errors="replace") if solution.exists() else "")
        t["timeout_sec"] = int(meta.get("timer", {}).get("minutes", 30) * 60) if isinstance(meta.get("timer"), dict) else 1800
        t["_origin"] = {"repo_task_dir": str(tdir), "meta": meta}
        tasks.append(t)
    return tasks


def import_cvebench(source: str, repos: Path) -> list[dict]:
    """每题: src/critical/challenges/CVE-*/ + metadata/CVE-*.yml"""
    base = repos / "cve-bench" / "src" / "critical"
    challenges = base / "challenges"
    metas = base / "metadata"
    tasks = []
    for i, cdir in enumerate(sorted(p for p in challenges.iterdir() if p.is_dir()), 1):
        cve = cdir.name
        yml = metas / f"{cve}.yml"
        meta = {}
        if yml.exists():
            try:
                meta = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
            except Exception:
                meta = {}
        app = str(meta.get("application_url", ""))
        t = new_task(
            source=source,
            source_id=cve,
            source_url=f"https://nvd.nist.gov/vuln/detail/{cve}",
            created_at=cve.split("-")[1] or "",   # CVE 编号年份，粗粒度
            language="web",
            repo=str(meta.get("app_name", cve)),
            task_type="security",
            problem_statement=f"在本地环境中复现并利用/修复漏洞 {cve}。应用: {app}。攻击目标类型见 metadata。",
            verifier_type="endtoend",   # inspect 评分器，多目标二值
            env_type="docker_build",
            env_notes="每题一个 compose 栈，cvebench/ 命名空间或本地 build；本地隔离运行",
        )
        t["bank_id"] = bank_id(source, i)
        t["gold_seal"] = seal(t["bank_id"], "reference exploit available on request")
        t["_origin"] = {"challenge_dir": str(cdir), "metadata": meta}
        tasks.append(t)
    return tasks


def import_bugsinpy(source: str, repos: Path) -> list[dict]:
    """每题: projects/<proj>/bugs/<n>/{bug.info, bug_patch.txt, run_test.sh}"""
    base = repos / "bugsinpy" / "projects"
    tasks = []
    i = 0
    for proj_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        bugs_dir = proj_dir / "bugs"
        if not bugs_dir.exists():
            continue
        for bug_dir in sorted(p for p in bugs_dir.iterdir() if p.is_dir()):
            info = bug_dir / "bug.info"
            patch = bug_dir / "bug_patch.txt"
            if not info.exists():
                continue
            i += 1
            meta = {}
            for line in info.read_text(encoding="utf-8", errors="replace").splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip()] = v.strip()
            t = new_task(
                source=source,
                source_id=f"{proj_dir.name}-{bug_dir.name}",
                language="python",
                repo=proj_dir.name,
                task_type="bug_fix",
                problem_statement=f"修复 {proj_dir.name} 中 bug #{bug_dir.name}：使触发测试通过。测试命令见 run_test.sh。",
                verifier_type="f2p_p2p_tests",
                env_type="venv",
                env_notes="bugsinpy-checkout/compile/test 流程，Python 3.8 venv",
                notes="license_unverified_internal_only",
            )
            t["created_at"] = ""
            t["bank_id"] = bank_id(source, i)
            t["gold_seal"] = seal(t["bank_id"], patch.read_text(encoding="utf-8", errors="replace") if patch.exists() else "")
            t["_origin"] = {"bug_dir": str(bug_dir), "info": meta}
            tasks.append(t)
    return tasks


def import_swelancer(source: str, repos: Path) -> list[dict]:
    """swelancer_tasks.csv（question_id, variant, price, prompt...）+ issues/*/issue_data.json"""
    root = repos / "swelancer"
    csv = root / "swelancer_tasks.csv"
    if not csv.exists():
        raise FileNotFoundError(f"{csv} 不存在（需旧提交 999bc21e）")
    df = pd.read_csv(csv)
    issues_dir = root / "issues"
    tasks = []
    for i, row in df.iterrows():
        qid = str(row.get("question_id", f"row{i}"))
        variant = str(row.get("variant", ""))
        price = row.get("price", 0)
        issue_data = issues_dir / qid / "issue_data.json"
        ps = str(row.get("prompt", ""))[:8000]
        if issue_data.exists():
            try:
                d = json.loads(issue_data.read_text(encoding="utf-8"))
                ps = (str(d.get("title", "")) + "\n\n" + str(d.get("body", "")))[:8000] or ps
            except Exception:
                pass
        t = new_task(
            source=source,
            source_id=qid,
            language="typescript",   # Expensify monorepo 为主
            repo="expensify/app",
            task_type="feature",
            problem_statement=ps,
            verifier_type="endtoend",   # 端到端测试
            env_type="docker_image",
            image_ref="swelancer:local",   # 共用镜像，本地构建一次
            image_registry="local-build",
            env_notes=f"variant={variant}; 悬赏=${price}; 共用 Expensify 镜像",
            timeout_sec=3600,
        )
        t["bank_id"] = bank_id(source, i + 1)
        t["gold_seal"] = seal(t["bank_id"], "")
        t["_origin"] = {"question_id": qid, "variant": variant, "price": float(price) if price == price else 0.0}
        tasks.append(t)
    return tasks


IMPORTERS = {
    "terminalbench1": import_terminalbench1,
    "cvebench": import_cvebench,
    "bugsinpy": import_bugsinpy,
    "swelancer": import_swelancer,
}


def run_imports(only: str | None = None) -> dict[str, list[dict]]:
    from env import REPOS
    out = {}
    for source, fn in IMPORTERS.items():
        if only and only not in source:
            continue
        try:
            rows = fn(source, REPOS)
            out[source] = rows
            print(f"[ok] {source}: {len(rows)} 题")
        except Exception as e:
            print(f"[FAIL] {source}: {type(e).__name__}: {e}")
    return out
