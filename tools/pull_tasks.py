"""Phase 4 核心：按需拉题流水线。

  pull_tasks.py --view anchor --n 10 [--model glm-5.3] [--no-env] [--mirror docker.1ms.run]

流程：
  1. 读 index + 视图（或 --bank-ids / --where）选题；--model 时做防污染过滤
  2. 逐题物化 bank/tasks/<bank_id>/：
     - task.json     索引行（含判定配置）
     - origin.json   源元信息（base_commit/test_patch 等，无金标）
     - problem.md    给 agent 看的题面
  3. 环境层（--no-env 跳过）：
     - docker_image  → image_cache.pull（含镜像加速中转）
     - docker_build/conda/venv/none → 记录待构建标记，实际构建在 audit/运行时做
  4. 输出物化清单，交 audit_task.py 金标预检
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def select_ids(args) -> list[str]:
    import pandas as pd
    from common import load_index

    df = load_index()
    if args.bank_ids:
        ids = args.bank_ids.split(",")
    elif args.where:
        ids = df.query(args.where)["bank_id"].tolist()
    else:
        view_file = Path(__file__).parent.parent / "bank" / "views" / f"{args.view}.json"
        ids = json.loads(view_file.read_text(encoding="utf-8"))
    if args.model:
        ok = df["cutoff_ok_json"].map(
            lambda s: json.loads(s).get(args.model, {}).get("ok", False))
        allowed = set(df[ok]["bank_id"])
        before = len(ids)
        ids = [i for i in ids if i in allowed]
        print(f"防污染过滤[{args.model}]: {before} -> {len(ids)}")
    sub = df[df["bank_id"].isin(ids)]
    if not sub.empty and args.task_type:
        sub = sub[sub["task_type"] == args.task_type]
    if args.n and len(sub) > args.n:
        sub = sub.sample(args.n, random_state=42)
    return sub["bank_id"].tolist()


def materialize(task: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {k: v for k, v in task.items() if k != "problem_statement"}
    (out_dir / "task.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    ps = task.get("problem_statement") or "（该源无题面文本，见 task.json）"
    header = (
        f"# {task['bank_id']}（{task['source']} / {task['repo'] or task['language']}）\n\n"
        f"类型: {task['task_type']} | 语言: {task['language']}\n\n---\n\n"
    )
    (out_dir / "problem.md").write_text(header + ps, encoding="utf-8")


def prep_env(task: dict, mirror: str | None, anchor: bool) -> str:
    """返回环境状态: pulled / cached / pending_build / skip"""
    import image_cache
    et = task["env_type"]
    if et == "docker_image" and task.get("image_ref"):
        ref = task["image_ref"]
        if image_cache.has(ref):
            image_cache.touch(ref)
            return "cached"
        if not image_cache.docker_available():
            return "pending_pull(docker未就绪)"
        okp = image_cache.pull(ref, task["bank_id"], anchor=anchor, registry_mirror=mirror)
        return "pulled" if okp else "pull_failed"
    if et in ("docker_build", "conda", "venv", "docker_image"):
        return f"pending({et})"
    return "skip"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--view", default="fresh_all")
    ap.add_argument("--bank-ids", help="逗号分隔")
    ap.add_argument("--where", help="pandas.query 表达式")
    ap.add_argument("-n", "--n", type=int, default=0)
    ap.add_argument("--model", help="按模型截止日过滤")
    ap.add_argument("--task-type", dest="task_type")
    ap.add_argument("--no-env", action="store_true", help="只物化元数据，不碰环境")
    ap.add_argument("--mirror", default=None, help="Docker Hub 镜像加速前缀，如 docker.1ms.run")
    ap.add_argument("--anchor", action="store_true", help="拉到的镜像登记为锚题（永不淘汰）")
    args = ap.parse_args()

    import pandas as pd
    from common import load_index
    from env import TASKS

    ids = select_ids(args)
    if not ids:
        print("没有选中任何题")
        return 1
    df = load_index()
    rows = df[df["bank_id"].isin(ids)].set_index("bank_id")
    print(f"选中 {len(ids)} 题，开始物化")
    stats: dict[str, int] = {}
    report = []
    for bid in ids:
        t = rows.loc[bid].to_dict()
        materialize(t, TASKS / bid)
        env_state = "skip" if args.no_env else prep_env(t, args.mirror, args.anchor)
        stats[env_state] = stats.get(env_state, 0) + 1
        report.append({"bank_id": bid, "env": env_state, "image": t.get("image_ref", "")})
        print(f"  {bid:<14} {t['source']:<18} {env_state}")
    from env import LOGS
    (LOGS / "last_pull_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("环境状态分布:", stats)
    print("下一步: uv run python tools/audit_task.py --report logs/last_pull_report.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
