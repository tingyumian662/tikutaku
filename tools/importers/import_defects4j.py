"""导入器：Defects4J v3 —— 解析 framework/projects/<PID>/active-bugs.csv（无需 Java）。

金标 patch 与触发测试需要 CLI（Java 11）导出，本导入器先建索引骨架：
  - f2p 留空 + notes 标注 enrich_pending
  - 环境登记官方 Docker 镜像 rjust/defects4j（Phase 2 用）
  - Phase 2 补跑: defects4j export -p fix.patch / tests.trigger 回填并重新封存金标
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import bank_id  # noqa: E402
from seal_gold import seal  # noqa: E402
from schema import new_task  # noqa: E402

# 项目编号 → 仓库名/语言（v3 主要为 Java）
D4J_META = {
    "Chart": "jfreechart", "Cli": "commons-cli", "Closure": "closure-compiler",
    "Codec": "commons-codec", "Collections": "commons-collections",
    "Compress": "commons-compress", "Csv": "commons-csv", "Dbutils": "commons-dbutils",
    "Gson": "gson", "JacksonCore": "jackson-core", "JacksonDatabind": "jackson-databind",
    "JacksonXml": "jackson-dataformat-xml", "Jsoup": "jsoup", "Jxpath": "commons-jxpath",
    "Lang": "commons-lang", "Math": "commons-math", "Mockito": "mockito", "Time": "joda-time",
}


def import_defects4j(source: str, repos: Path) -> list[dict]:
    base = repos / "defects4j" / "framework" / "projects"
    tasks = []
    for proj_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        csv_path = proj_dir / "active-bugs.csv"
        if not csv_path.exists():
            continue
        pid = proj_dir.name
        with csv_path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                bug_id = (row.get("bug.id") or "").strip()
                if not bug_id:
                    continue
                t = new_task(
                    source=source,
                    source_id=f"{pid}-{bug_id}",
                    language="java",
                    repo=D4J_META.get(pid, pid),
                    task_type="bug_fix",
                    problem_statement=f"修复 {D4J_META.get(pid, pid)} 的缺陷 #{bug_id}（Defects4J {pid} 项目），使触发测试通过。",
                    verifier_type="f2p_p2p_tests",
                    env_type="venv",
                    image_ref="",
                    image_registry="",
                    env_notes="本地路线（无 Docker）：便携 JDK11 + defects4j CLI + 本地 git bundle",
                    fail_to_pass="[]",
                    notes="enrich_pending: 需 defects4j export 导出 fix.patch 与 tests.trigger 回填",
                )
                t["created_at"] = ""
                t["_origin"] = {"project": pid, "bug_id": bug_id, "raw": {k: v for k, v in row.items()}}
                t["bank_id"] = bank_id(source, len(tasks) + 1)
                t["gold_seal"] = seal(t["bank_id"], "")
                tasks.append(t)
    return tasks


def run_imports(only: str | None = None) -> dict[str, list[dict]]:
    from env import REPOS
    if only and only not in "defects4j":
        return {}
    try:
        rows = import_defects4j("defects4j", REPOS)
        print(f"[ok] defects4j: {len(rows)} 题")
        return {"defects4j": rows}
    except Exception as e:
        print(f"[FAIL] defects4j: {type(e).__name__}: {e}")
        return {}
