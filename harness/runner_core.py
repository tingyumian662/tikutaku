"""可被 Web 前端调用的批量运行核心：事件回调 + 停止信号 + 历史汇总。

复用 run.py 的 load_selection / run_one / write_leaderboard，保证 CLI 与网页跑出的
results.jsonl 完全同构（backfill_calibration / --skip-done 直接兼容）。
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "tools"))

import run as run_mod  # noqa: E402
from env import RESULTS  # noqa: E402

VERDICTS_FINAL = ("pass", "wrong_answer", "timeout", "compile_error", "apply_fail")

# ---------- 出题池与难度档 ----------
# 只收录"有答题渠道 + 有金标/测试 + 可自动判分"的题；新源接好判分器后加入此处即可入池。
ELIGIBLE_VERIFIERS = {"oj_tests", "d4j_tests", "bip_tests", "efficiency_test"}

# 难度档映射：N1 最难 … N5 最易（用户口径），库内标定 level L1 最易 … L5 最难。
TIER_OF_LEVEL = {"L1": "N5", "L2": "N4", "L3": "N3", "L4": "N2", "L5": "N1"}
TIERS = ["N1", "N2", "N3", "N4", "N5"]

# 试卷域：每域独立难度尺（方法论红线：不跨域共用尺）。
# 三源（D4J/BIP/Perf）2026-10-17 播种为独立 3 档（L1 快pass/L2 慢pass/L3 失败），
# 映射 N4易/N3中/N2难；N1/N5 无库存，组卷缺口自动从其余档补。
DOMAINS = {
    "algo":     {"label": "算法（LiveCodeBench）", "sources": ["livecodebench"]},
    "java_fix": {"label": "Java 修码（Defects4J）", "sources": ["defects4j"]},
    "py_fix":   {"label": "Python 修码（BugsInPy）", "sources": ["bugsinpy"]},
    "perf":     {"label": "性能优化（SWE-Perf）", "sources": ["sweperf"]},
}
_TIER3 = {"L1": "N4", "L2": "N3", "L3": "N2"}
LEVEL_OF_SOURCE = {"livecodebench": TIER_OF_LEVEL, "defects4j": _TIER3,
                   "bugsinpy": _TIER3, "sweperf": _TIER3}

_POOL_CACHE: dict[str, dict[str, list[str]]] = {}


def _pool(domain: str = "algo") -> dict[str, list[str]]:
    """可测题池，按域过滤后按难度档分桶（bank_id 列表）。"""
    if domain not in _POOL_CACHE:
        import pandas as pd
        df = pd.read_parquet(ROOT / "bank" / "index.parquet",
                             columns=["bank_id", "source", "verifier_type", "level",
                                      "fail_to_pass", "retired"])
        ok = df[(df["verifier_type"].isin(ELIGIBLE_VERIFIERS)) & df["fail_to_pass"].notna()
                & (df["source"].isin(DOMAINS[domain]["sources"]))
                & (~df["retired"].astype(bool) if "retired" in df.columns else True)]
        buckets: dict[str, list[str]] = {t: [] for t in TIERS}
        for src, lv, ids in zip(ok["source"], ok["level"], ok["bank_id"]):
            tier = LEVEL_OF_SOURCE.get(str(src), {}).get(str(lv))
            if tier:
                buckets[tier].append(ids)
        _POOL_CACHE[domain] = buckets
    return _POOL_CACHE[domain]


def pool_stats() -> dict:
    return {"domains": {d: {"label": DOMAINS[d]["label"],
                            "tiers": {t: len(v) for t, v in _pool(d).items()},
                            "total": sum(len(v) for v in _pool(d).values())}
                        for d in DOMAINS}}


def compose_paper(total: int, ratios: list, seed=None, domain: str = "algo") -> tuple[list[str], dict, dict]:
    """按 [N1..N5] 占比随机组卷（限域）。返回 (bank_ids, 实际分配, 缺口说明)。

    某档库存不足时全取并从其余档按剩余库存补齐；seed 固定则组卷可复现。
    """
    import random
    rng = random.Random(seed)
    b = _pool(domain)
    total = max(1, min(int(total), sum(len(v) for v in b.values())))
    s = float(sum(ratios)) or 1.0
    share = [max(0.0, float(r)) / s for r in ratios[:5]] + [0.0] * max(0, 5 - len(ratios))

    # 最大余数法取整
    raw = [total * x for x in share]
    want = [int(x) for x in raw]
    order = sorted(range(5), key=lambda i: raw[i] - want[i], reverse=True)
    for i in range(total - sum(want)):
        want[order[i % 5]] += 1

    picked: list[str] = []
    alloc, shortfall = {}, {}
    for i, t in enumerate(TIERS):
        stock = len(b[t])
        take = min(want[i], stock)
        if want[i] > stock:
            shortfall[t] = {"want": want[i], "stock": stock}
        chosen = rng.sample(b[t], take) if take else []
        alloc[t] = take
        picked += chosen
    # 缺口从剩余库存大的档补
    left = total - len(picked)
    if left > 0:
        remain = {t: [x for x in b[t] if x not in set(picked)] for t in TIERS}
        for t in sorted(TIERS, key=lambda t: -len(remain[t])):
            if left <= 0:
                break
            take = min(left, len(remain[t]))
            alloc[t] += take
            picked += rng.sample(remain[t], take)
            left -= take
    rng.shuffle(picked)
    return picked, alloc, shortfall


class BatchRun:
    """一次评测批次。start() 后台线程执行；stop() 在题与题之间打断。"""

    def __init__(self, cfg: dict):
        self.cfg = dict(cfg)
        self.stop = threading.Event()
        self.status = "idle"           # running / finished / stopped / error
        self.error: str | None = None
        self.run_dir: Path | None = None
        self.records: list[dict] = []
        self.total = 0
        self.t0 = 0.0
        self._tend: float | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        # cfg: view/n/bank_ids/skip_done/model/base_url/api_key/protocol/concurrency

    # ---- 事件回调：web 层注入 ----
    def on_event(self, ev: dict) -> None:  # pragma: no cover - 由子类/外部替换
        pass

    def _emit(self, etype: str, **kw) -> None:
        ev = {"type": etype, "ts": round(time.time(), 1)}
        ev.update(kw)
        try:
            self.on_event(ev)
        except Exception:
            pass

    # ---- 生命周期 ----
    def start(self) -> None:
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def request_stop(self) -> None:
        self.stop.set()

    def _worker(self) -> None:
        try:
            self._worker_inner()
        except Exception as e:  # noqa: BLE001
            self.status = "error"
            self.error = f"{type(e).__name__}: {e}"[:500]
            self._emit("error", msg=self.error)
        finally:
            self._tend = time.time()

    def _worker_inner(self) -> None:
        self.t0 = time.time()
        self.status = "running"
        bank_ids = self.cfg.get("bank_ids")
        alloc, shortfall = None, None
        if not self.cfg.get("view") and not bank_ids and self.cfg.get("total"):
            bank_ids, alloc, shortfall = compose_paper(
                int(self.cfg["total"]), self.cfg.get("ratios") or [20, 30, 30, 15, 5],
                seed=self.cfg.get("seed"), domain=self.cfg.get("domain") or "algo")
            self.paper_alloc = alloc
            self.paper_shortfall = shortfall
        ns = argparse.Namespace(
            view=self.cfg.get("view"), report=None, bank_ids=(",".join(bank_ids) if isinstance(bank_ids, list) else bank_ids),
            n=int(self.cfg.get("n") or 0), skip_done=bool(self.cfg.get("skip_done")),
            adapter="api", model=self.cfg.get("model"), base_url=self.cfg.get("base_url"),
            api_key=self.cfg.get("api_key"), protocol=self.cfg.get("protocol") or "auto",
            tool=None, concurrency=int(self.cfg.get("concurrency") or 1),
            max_tokens=self.cfg.get("max_tokens"), temperature=self.cfg.get("temperature"),
            task_timeout=self.cfg.get("task_timeout"), score_timeout=self.cfg.get("score_timeout"),
            system_extra=self.cfg.get("system_extra"),
            thinking_level=self.cfg.get("thinking_level") or "max",
        )
        tasks = run_mod.load_selection(ns)
        self.total = len(tasks)
        # 档位标签按题目所属源的独立尺映射（LCB 五档 / 三源三档），与组卷池同一张表
        self._tiers = {t["bank_id"]: (LEVEL_OF_SOURCE.get(str(t.get("source") or ""),
                             TIER_OF_LEVEL).get(str(t.get("level") or ""))
                             or {"easy": "N4", "medium": "N3", "hard": "N2"}.get(
                                 str(t.get("initial_difficulty") or "").lower(), "unrated"))
                       for t in tasks if t.get("bank_id")}
        run_id = time.strftime("run-%Y%m%d-%H%M%S")
        self.run_dir = RESULTS / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "config.json").write_text(
            json.dumps({k: v for k, v in self.cfg.items() if k != "api_key"}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        self._emit("meta", run_id=run_id, total=self.total,
                   model=self.cfg.get("model"), view=self.cfg.get("view"),
                   paper_total=(len(bank_ids) if isinstance(bank_ids, list) else None),
                   alloc=alloc, shortfall=shortfall,
                   selected_before_skip=(len(bank_ids) if isinstance(bank_ids, list) else self.total))
        if not tasks:
            self.status = "finished"
            self._emit("finished", summary=self.summary(),
                       note=("选题全部被「跳过已完成」过滤：该模型此前已测完这些题；"
                             "换模型或关闭跳过开关即可") if isinstance(bank_ids, list) and len(bank_ids) else None)
            return

        out = (self.run_dir / "results.jsonl").open("a", encoding="utf-8")

        def safe_run_one(task: dict) -> dict:
            try:
                return run_mod.run_one(task, ns)
            except Exception as e:  # noqa: BLE001
                return {"bank_id": task.get("bank_id"), "model": ns.model, "source": task.get("source"),
                        "score": {"verdict": f"error:{type(e).__name__}"}, "error": str(e)[:300],
                        "total_sec": 0}

        def on_done(rec: dict) -> None:
            with self._lock:
                rec.setdefault("difficulty", self._tiers.get(rec.get("bank_id"), "unknown"))
                self.records.append(rec)
                out.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
                out.flush()
            self._emit("task_done", rec=rec)

        conc = max(1, min(int(self.cfg.get("concurrency") or 1), 8))
        if conc == 1:
            for task in tasks:
                if self.stop.is_set():
                    break
                self._emit("task_start", bank_id=task["bank_id"])
                on_done(safe_run_one(task))
        else:
            with ThreadPoolExecutor(max_workers=conc) as pool:
                futs, it = set(), iter(tasks)
                while True:
                    while not self.stop.is_set() and len(futs) < conc:
                        try:
                            task = next(it)
                        except StopIteration:
                            break
                        self._emit("task_start", bank_id=task["bank_id"])
                        futs.add(pool.submit(safe_run_one, task))
                    if not futs:
                        break
                    done, futs = wait(futs, return_when=FIRST_COMPLETED, timeout=1.0)
                    for f in done:
                        on_done(f.result())
        out.close()
        run_mod.write_leaderboard(self.run_dir, self.records)
        self.status = "stopped" if self.stop.is_set() else "finished"
        self._emit("finished", summary=self.summary())

    # ---- 汇总 ----
    def summary(self) -> dict:
        return summarize(self.records)

    def snapshot(self) -> dict:
        with self._lock:
            recs = list(self.records)
        return {
            "status": self.status, "error": self.error,
            "run_id": self.run_dir.name if self.run_dir else None,
            "total": self.total,
            "elapsed": round((self._tend or time.time()) - self.t0, 1) if self.t0 else 0,
            "summary": summarize(recs),
        }


# ---------- 报告聚合（当前批次与历史共用） ----------

def summarize(records: list[dict], tier_of=None) -> dict:
    """按判定 + 难度汇总。tier_of: bank_id -> easy/medium/hard/unknown（历史扫描时注入）。"""
    if tier_of is None:
        tier_of = _tier_from_index()
    tiers: dict[str, dict] = {}
    verdicts: dict[str, int] = {}
    sec, tok = 0.0, 0
    for r in records:
        v = (r.get("score") or {}).get("verdict", "?")
        base = v.split(":")[0]
        verdicts[base] = verdicts.get(base, 0) + 1
        if base == "error":
            continue   # 作废测量（如空回复）不进难度档统计，避免"未测"被读成"测错"
        t = tier_of(r.get("bank_id", "")) or "unknown"
        cell = tiers.setdefault(t, {"n": 0, "pass": 0})
        cell["n"] += 1
        if base == "pass":
            cell["pass"] += 1
        sec += float(r.get("total_sec") or 0)
        u = r.get("usage") or {}
        tok += int(u.get("prompt_tokens", 0) or 0) + int(u.get("completion_tokens", 0) or 0)
    n = len(records)
    out = {"n": n, "pass": verdicts.get("pass", 0), "rate": round(verdicts.get("pass", 0) / n, 4) if n else 0,
           "verdicts": verdicts, "tiers": tiers, "avg_sec": round(sec / n, 1) if n else 0,
           "total_tokens": tok}
    for t in list(TIERS) + ["unrated"]:
        c = tiers.get(t)
        if c:
            c["rate"] = round(c["pass"] / c["n"], 4)
    return out


_TIER_CACHE: dict | None = None


def _tier_from_index():
    """bank_id → N1..N5 档（取库内 level；无 level 的旧记录回落 initial_difficulty 粗映射）。"""
    global _TIER_CACHE
    if _TIER_CACHE is None:
        try:
            import pandas as pd
            df = pd.read_parquet(ROOT / "bank" / "index.parquet",
                                 columns=["bank_id", "level", "initial_difficulty"])
            coarser = {"easy": "N4", "medium": "N3", "hard": "N2"}
            _TIER_CACHE = {
                bid: TIER_OF_LEVEL.get(str(lvl), coarser.get(str(d).lower(), "unrated"))
                for bid, lvl, d in zip(df["bank_id"], df["level"], df["initial_difficulty"])
            }
        except Exception:
            _TIER_CACHE = {}
    m = _TIER_CACHE
    return lambda bid: m.get(bid, "unrated")


def scan_history(limit: int = 60) -> dict:
    """遍历 results/run-*/results.jsonl → {"runs": [每个 run 一行], "models": {model: 合并汇总}}。

    同一模型多 run 合并时，新 run 的判定覆盖旧 run（skip-done 重试后以最新为准）。
    """
    run_dirs = sorted(RESULTS.glob("run-*"), reverse=True)[:limit]
    tier_of = _tier_from_index()
    per_model: dict[str, dict[str, dict]] = {}
    run_rows = []
    for d in run_dirs:
        f = d / "results.jsonl"
        if not f.exists():
            continue
        recs = []
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                recs.append(json.loads(line))
            except Exception:
                continue
        if not recs:
            continue
        model = recs[0].get("model", "?")
        run_rows.append({"run_id": d.name, "model": model, "n": len(recs),
                         **summarize(recs, tier_of)})
    for d in reversed(run_dirs):  # 旧 run 先入，新 run 后入 → 新判定覆盖旧判定
        f = d / "results.jsonl"
        if not f.exists():
            continue
        model = None
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            model = model or r.get("model", "?")
            per_model.setdefault(model, {})[r.get("bank_id")] = r
    models = {}
    for model, bucket in per_model.items():
        models[model] = summarize(list(bucket.values()), tier_of)
    return {"runs": run_rows, "models": models}
