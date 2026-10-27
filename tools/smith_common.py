"""SWE-smith 式自产题流水线公共件：jsonl 读写、usage 记账、基线快照、硬校验。

2026-10-26 计划落点：
- 模型分工（用户拍板）：生成 glm-5.3 满血、质检 glm-5.3-flash、标定 glm-5.3；
  DeepSeek 不经明确指令不调用；GLM 并发红线 ≤2（1302 教训），只在白天跑
- 双向鉴别：fixed 干净基线全过 + 注入版跑同测试必有失败（失败节点即 F2P）
- 金标 = 原干净文件，seal 封 unified diff（带 diff --git 头，d4j_gold_paths 可解析路径）
- 入库走增量 concat，绝不走 build_index（全量重建丢 tier_rank/level）
- 隔离：source=swesmith_local 不在 runner_core.DOMAINS 里，天然不入池/锚卷/榜单
"""
from __future__ import annotations

import ast
import difflib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "tools"))

SMITH_DIR = ROOT / "logs" / "smith"
CANDIDATES = SMITH_DIR / "candidates.jsonl"
INJECTED = SMITH_DIR / "injected.jsonl"
VERIFIED = SMITH_DIR / "verified.jsonl"
AUDITED = SMITH_DIR / "audited.jsonl"
USAGE = SMITH_DIR / "usage.json"
BASELINE_CACHE = SMITH_DIR / "baseline_cache.json"

SOURCE_KEY = "swesmith_local"   # 已登记 tools/importers/common.py SOURCES，前缀 SYN
PREFIX = "SYN"

GENERATOR = "glm-5.3"          # 满血生成（anthropic 协议，凭据 ~/.claude/settings.json）
QC_MODEL = "glm-5.3-flash"     # 质检
MAX_CHANGED_LINES = 10         # 单文件改动行数上限（SWE-smith 语义：≤K 行）


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


class UsageLedger:
    """全程订阅量记账（现金 ¥0），持久化 usage.json，崩溃不丢。"""

    def __init__(self) -> None:
        self.data: dict = json.loads(USAGE.read_text(encoding="utf-8")) if USAGE.exists() else {}

    def add(self, model: str, usage: dict | None) -> None:
        if not usage:
            return
        rec = self.data.setdefault(model, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
        rec["calls"] += 1
        rec["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        rec["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        USAGE.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")

    def summary(self) -> str:
        return " | ".join(
            f"{m}:{v['calls']}次 {v['prompt_tokens']/1e6:.2f}M入 {v['completion_tokens']/1e6:.2f}M出"
            for m, v in self.data.items()) or "无调用"


def next_seq() -> int:
    """下一个 SYN 序号（入库时调用；ingest 侧幂等复核防撞）。"""
    import pandas as pd
    from env import INDEX
    df = pd.read_parquet(INDEX, columns=["bank_id"])
    nums = [int(b.split("-")[1]) for b in df["bank_id"] if b.startswith(PREFIX + "-")]
    return (max(nums) + 1) if nums else 1


def fixed_snapshot(parent_bid: str, repo_url: str, fixed_commit: str) -> Path | None:
    """父题 fixed 快照（= 合成题干净基线）。缓存命中直接用，否则 codeload 拉取。"""
    from bip_gold_audit import FIXED_ROOT, fetch_tar
    dest = FIXED_ROOT / parent_bid
    if dest.exists() and any(dest.iterdir()):
        return dest
    return fetch_tar(repo_url, fixed_commit, dest)


# ---------------- 注入硬校验（M2/M4 复用） ----------------

def _sig_map(code: str) -> dict[str, str]:
    """顶层函数/类方法的签名指纹：任何签名/装饰器/类基变化都会被检出。"""
    tree = ast.parse(code)
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[f"f:{node.name}"] = (ast.dump(node.args) + "|" + ast.dump(node.returns or ast.Constant(value=None))
                                     + "|" + ",".join(ast.dump(d) for d in node.decorator_list))
        elif isinstance(node, ast.ClassDef):
            out[f"c:{node.name}"] = (",".join(ast.dump(b) for b in node.bases)
                                      + "|" + ",".join(ast.dump(d) for d in node.decorator_list))
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out[f"m:{node.name}.{sub.name}"] = (ast.dump(sub.args) + "|" + ast.dump(
                        sub.returns or ast.Constant(value=None)))
    return out


def changed_lines(old: str, new: str) -> int:
    """unified diff 的 +/- 行数（剔除文件头），即"改动行数"。"""
    diff = difflib.unified_diff(old.splitlines(), new.splitlines(), n=0)
    return sum(1 for l in diff if (l.startswith("+") or l.startswith("-")) and not l.startswith(("+++", "---")))


def check_injection(path: str, old: str, new: str) -> str | None:
    """返回 None=通过；否则返回拒绝原因。单文件、语法合法、签名不变、≤K 行、确有改动。"""
    if not new.strip():
        return "empty_file"
    if not path.endswith(".py"):
        return "not_python"
    try:
        ast.parse(new)
    except SyntaxError as e:
        return f"syntax_error:{e.lineno}"
    if _sig_map(old) != _sig_map(new):
        return "signature_changed"
    n = changed_lines(old, new)
    if n == 0:
        return "no_change"
    if n > MAX_CHANGED_LINES:
        return f"too_many_changes:{n}"
    return None


def seal_diff(path: str, buggy: str, clean: str) -> str:
    """合成题金标文本：带 diff --git 头的 unified diff（buggy→clean）。

    d4j_gold_paths 靠 `diff --git a/(\\S+) b/(\\S+)` 提取路径，判分链零改动。
    """
    body = difflib.unified_diff(buggy.splitlines(), clean.splitlines(),
                                fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="")
    return f"diff --git a/{path} b/{path}\n" + "\n".join(body) + "\n"
