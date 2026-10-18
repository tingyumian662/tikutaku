"""导入器：Terminal-Bench 2.0 / 2.1（HF 数据集为逐题目录: <task>/{instruction.md, task.toml, environment/, solution/, tests/}）。"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import bank_id  # noqa: E402
from seal_gold import seal  # noqa: E402
from schema import new_task  # noqa: E402


def import_tb2(source: str, dirname: str, data_root: Path) -> list[dict]:
    base = data_root / dirname
    task_dirs = sorted(p for p in base.iterdir()
                       if p.is_dir() and not p.name.startswith(".") and (p / "instruction.md").exists())
    tasks = []
    for i, tdir in enumerate(task_dirs, 1):
        ps = (tdir / "instruction.md").read_text(encoding="utf-8", errors="replace")[:8000]
        meta = {}
        toml = tdir / "task.toml"
        if toml.exists():
            try:
                meta = tomllib.loads(toml.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        # solution 可能是目录（solve.sh 等）
        sol_dir = tdir / "solution"
        gold = ""
        if sol_dir.is_dir():
            gold = "\n".join(f"--- {p.name} ---\n{p.read_text(encoding='utf-8', errors='replace')}"
                             for p in sorted(sol_dir.rglob("*")) if p.is_file())[:20000]
        elif (tdir / "solution.sh").exists():
            gold = (tdir / "solution.sh").read_text(encoding="utf-8", errors="replace")
        t = new_task(
            source=source,
            source_id=tdir.name,
            language="shell",
            repo="terminal-bench-2",
            task_type="terminal",
            problem_statement=ps,
            verifier_type="reward_script",     # tests/ 产出 /logs/verifier/reward.txt
            env_type="docker_build",
            env_notes=f"本地构建: {dirname}/{tdir.name}/environment",
            timeout_sec=int(meta.get("task", {}).get("timer", {}).get("max", 30)) * 60
                        if isinstance(meta.get("task", {}).get("timer"), dict) else 1800,
        )
        t["bank_id"] = bank_id(source, i)
        t["gold_seal"] = seal(t["bank_id"], gold)
        t["_origin"] = {"dataset_task_dir": str(tdir), "meta": meta}
        tasks.append(t)
    return tasks


IMPORTERS = {
    "terminalbench2": import_tb2,
}


def run_imports(only: str | None = None) -> dict[str, list[dict]]:
    from env import DATASETS
    out = {}
    for source, fn in IMPORTERS.items():
        for dirname, src in (("terminal-bench_2.0", "terminalbench2"),):
            if only and only not in src:
                continue
            if not (DATASETS / dirname / ".done").exists():
                print(f"[skip] {dirname} 未下载完成")
                continue
            try:
                rows = fn(src, dirname, DATASETS)
                out[src] = rows
                print(f"[ok] {src}: {len(rows)} 题")
            except Exception as e:
                print(f"[FAIL] {src}: {type(e).__name__}: {e}")
    return out
