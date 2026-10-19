"""锚卷（排位赛固定卷）播种工具。

锚卷 = 一组冻结的 bank_id + 生成时刻的难度档快照。冻结语义：同名锚卷文件
已存在即报错，永不覆盖；轮换（如每季度）= 新建 anchor-v2 文件，旧卷保留
供历史对比（跨版本卷不直接比分，榜单按 anchor_id 分组）。

排位赛 run 的 config.json 记 anchor_id；榜单/时间序列/漂移检测均按
anchor_id 聚合（harness/runner_core.anchor_history）。

用法:
  # 100 题四域卷（algo:40 java_fix:20 py_fix:25 perf:15）
  uv run python tools/make_anchor.py --name anchor-v1 --seed 20261019
  # 自定义配比（e2e 冒烟小卷）
  uv run python tools/make_anchor.py --name smoke --mix algo:3,py_fix:2 --seed 1
  # 列出已有锚卷
  uv run python tools/make_anchor.py --list
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "tools"))

import runner_core as rc  # noqa: E402

ANCHORS = ROOT / "bank" / "anchors"


def parse_mix(mix: str) -> dict[str, int]:
    """'algo:40,java_fix:20' → {'algo': 40, 'java_fix': 20}（域名校验 + 上限钳制）。"""
    out: dict[str, int] = {}
    for part in mix.split(","):
        part = part.strip()
        if not part:
            continue
        dom, _, n = part.rpartition(":")
        dom, n = dom.strip(), int(n)
        if dom not in rc.DOMAINS:
            raise SystemExit(f"未知域 {dom!r}；可用域: {', '.join(rc.DOMAINS)}")
        if n <= 0:
            raise SystemExit(f"域 {dom} 题数须为正整数")
        out[dom] = n
    if not out:
        raise SystemExit("--mix 不能为空，例如 algo:40,java_fix:20,py_fix:25,perf:15")
    return out


def make_anchor(name: str, mix: str, seed) -> dict:
    if not name.replace("-", "").replace("_", "").isalnum():
        raise SystemExit(f"锚卷名 {name!r} 只允许字母数字-_.")
    out_path = ANCHORS / f"{name}.json"
    if out_path.exists():
        raise SystemExit(f"锚卷已存在，冻结不可覆盖: {out_path}\n轮换请新建版本（如 {name.split('-')[0]}-v2）")

    want = parse_mix(mix)
    bank_ids: list[str] = []
    per_task: dict[str, dict] = {}
    domain_counts: dict[str, int] = {}
    for i, (dom, n) in enumerate(want.items()):
        # 域内难度档按库存自然占比抽（代表性）；每域独立 RNG（字符串种子）避免跨域相关
        pool = rc._pool(dom)
        stock = [len(pool[t]) for t in rc.TIERS]
        avail = sum(stock)
        if avail < n:
            raise SystemExit(f"域 {dom} 库存不足：要 {n}，只有 {avail}")
        picked, alloc, shortfall = rc.compose_paper(
            n, stock, seed=f"anchor:{seed}:{dom}", domain=dom)
        # 桶名反查 tier 快照
        tier_of = {bid: t for t, ids in pool.items() for bid in ids}
        dom_tiers: dict[str, int] = {}
        for bid in picked:
            t = tier_of.get(bid, "unknown")
            dom_tiers[t] = dom_tiers.get(t, 0) + 1
            per_task[bid] = {"tier": t, "domain": dom}
        bank_ids += picked
        domain_counts[dom] = len(picked)
        tiers_txt = " ".join(f"{t}×{dom_tiers.get(t, 0)}" for t in rc.TIERS if dom_tiers.get(t))
        print(f"  {dom:<9} {len(picked):>3} 题（{tiers_txt}）"
              + (f"  缺口补齐: {shortfall}" if shortfall else ""))

    doc = {
        "anchor_id": name,
        "created_at": time.strftime("%Y-%m-%d"),
        "seed": seed,
        "bank_ids": bank_ids,
        "per_task": per_task,
        "domain_counts": domain_counts,
        "notes": "冻结卷：生成后不可改；排位赛按本文件 bank_ids 出题，难度档用 per_task 快照",
    }
    ANCHORS.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"锚卷已生成: {out_path}（{len(bank_ids)} 题）")
    return doc


def list_anchors() -> None:
    if not ANCHORS.exists():
        print("尚无锚卷（bank/anchors/ 不存在）")
        return
    rows = 0
    for f in sorted(ANCHORS.glob("*.json")):
        try:
            a = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"  {f.name}  [读取失败: {e}]")
            continue
        mix = " ".join(f"{d}:{n}" for d, n in (a.get("domain_counts") or {}).items())
        print(f"  {a.get('anchor_id', f.stem):<16} {len(a.get('bank_ids') or []):>4} 题  "
              f"{a.get('created_at', '?')}  [{mix}]")
        rows += 1
    if not rows:
        print("尚无锚卷")


def main() -> int:
    ap = argparse.ArgumentParser(description="排位赛锚卷播种")
    ap.add_argument("--name", help="锚卷名，如 anchor-v1")
    ap.add_argument("--mix", default="algo:40,java_fix:20,py_fix:25,perf:15",
                    help="四域题量，默认 100 题大卷")
    ap.add_argument("--seed", default=None, help="随机种子（记录在锚卷内，默认当天日期）")
    ap.add_argument("--list", action="store_true", help="列出已有锚卷")
    args = ap.parse_args()
    if args.list:
        list_anchors()
        return 0
    if not args.name:
        ap.error("需要 --name 或 --list")
    seed = args.seed or time.strftime("%Y%m%d")
    make_anchor(args.name, args.mix, seed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
