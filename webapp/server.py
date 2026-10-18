"""本地测试台 Web 服务：配置模型 → 跑题 → 实时进度 → 评分报告。

启动:  uv run python webapp/server.py [--port 8790] [--no-open]
前端:  webapp/static/index.html（同源轮询 /api/state，无需构建步骤）
密钥:  只在内存中转发给 SDK，不写盘（run 目录里的 config.json 已剔除 api_key）。
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "tools"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import runner_core  # noqa: E402
from adapters import api_agent  # noqa: E402

app = FastAPI(title="题库测试台")

STATIC = Path(__file__).parent / "static"

VIEW_META = {
    "lcb_easy":       {"label": "算法题 · 简单（LiveCodeBench）", "api_ready": True},
    "lcb_medium":     {"label": "算法题 · 中等（LiveCodeBench）", "api_ready": True},
    "lcb_hard":       {"label": "算法题 · 困难（LiveCodeBench）", "api_ready": True},
    "runnable_all":   {"label": "全部可本地运行（含 SWE 待判分题）", "api_ready": False},
    "java_classic":   {"label": "Java 经典缺陷 Defects4J（API 模式仅出补丁）", "api_ready": False},
    "python_classic": {"label": "Python 经典缺陷（API 模式仅出补丁）", "api_ready": False},
    "perf":           {"label": "性能优化 SWE-Perf（API 模式仅出补丁）", "api_ready": False},
    "verified_sample": {"label": "SWE-bench Verified 抽样（需 Docker 判分）", "api_ready": False},
    "anchor_dormant": {"label": "新鲜锚题（需 Docker）", "api_ready": False},
    "dormant_docker": {"label": "Docker 休眠池（需 Docker）", "api_ready": False},
}

# ---------- 全局状态：同一时刻只允许一个批次 ----------
CURRENT: runner_core.BatchRun | None = None
EVENTS: list[dict] = []          # {"seq": n, ...事件体}
_EV_LOCK = threading.Lock()
_SEQ = 0


def _append_event(ev: dict) -> None:
    global _SEQ
    with _EV_LOCK:
        _SEQ += 1
        ev = {"seq": _SEQ, **ev}
        EVENTS.append(ev)
        if len(EVENTS) > 2000:
            del EVENTS[:1000]


class StartReq(BaseModel):
    model: str
    api_key: str = ""
    base_url: str = ""
    # 组卷（默认模式核心）：总题量 + N1..N5 占比，后端按库存随机抽取
    domain: str = "algo"         # 试卷域：algo/java_fix/py_fix/perf（独立难度尺）
    total: int = 30
    ratios: list[float] = [20, 30, 30, 15, 5]
    seed: int | None = None       # 固定种子 = 组卷可复现
    # 专家参数
    concurrency: int = 4
    skip_done: bool = True
    max_tokens: int = 131072     # 每题输出 token 上限（GLM 输出上限 128K，默认拉满；思考按需消耗）
    temperature: float = 0.2      # 仅旧式 openai 采样模型生效
    thinking_level: str = "max"   # 思考强度（评测默认拉满；映射各家参数，不认自动降级）
    task_timeout: float = 600     # 单题 API 请求时限（秒）
    score_timeout: int = 20       # 本地判分每测试点超时（秒）
    system_extra: str = ""        # 追加注入的系统提示词
    view: str = ""                # 兼容保留：非空则直接用原始视图（CLI/调试用）


class TestReq(BaseModel):
    model: str
    api_key: str = ""
    base_url: str = ""


def _require_conn(model: str, api_key: str, base_url: str) -> str | None:
    """三项必填；返回错误文案或 None。"""
    for name, v in (("模型名", model), ("API Key", api_key), ("接口地址", base_url)):
        if not v or not v.strip():
            return f"{name}为必填项，不能为空"
    return None


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/views")
def list_views():
    import json
    out = []
    for f in sorted((ROOT / "bank" / "views").glob("*.json")):
        meta = VIEW_META.get(f.stem, {"label": f.stem, "api_ready": False})
        try:
            n = len(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            n = 0
        out.append({"id": f.stem, "label": meta["label"], "n": n, "api_ready": meta["api_ready"]})
    return out


@app.get("/api/pool")
def pool():
    """可测题池（有判分渠道的题才入池）各难度档库存。"""
    return runner_core.pool_stats()


@app.post("/api/test")
def test_connection(req: TestReq):
    """自动探测协议并发一条极小请求验证连通性与鉴权；两个协议都不通则报错。"""
    miss = _require_conn(req.model, req.api_key, req.base_url)
    if miss:
        return {"ok": False, "latency_sec": 0, "error": miss}
    t0 = time.time()
    try:
        proto, info = api_agent.detect_protocol(req.model.strip(), req.base_url.strip(), req.api_key.strip())
        return {"ok": True, "protocol": proto, "latency_sec": round(time.time() - t0, 1),
                "reply": info["reply"], "usage": info["usage"]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "latency_sec": round(time.time() - t0, 1),
                "error": f"OpenAI 与 Anthropic 两种兼容协议均无法连通：{type(e).__name__}: {e}"[:500]}


@app.post("/api/start")
def start_run(req: StartReq):
    global CURRENT
    if CURRENT and CURRENT.status == "running":
        raise HTTPException(409, "已有批次在跑，先停止或等它结束")
    miss = _require_conn(req.model, req.api_key, req.base_url)
    if miss:
        raise HTTPException(400, miss)
    ratios = [max(0.0, float(r)) for r in (req.ratios or [])][:5]
    if not req.view and sum(ratios) <= 0:
        raise HTTPException(400, "难度配比不能全为 0")
    # 启动前探测协议：两个兼容协议都不通则直接报错，不进跑批
    try:
        proto, _ = api_agent.detect_protocol(req.model.strip(), req.base_url.strip(), req.api_key.strip())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"无法连接模型（OpenAI/Anthropic 协议均失败）：{type(e).__name__}: {e}"[:500])
    EVENTS.clear()

    cfg = {
        "view": req.view or None, "domain": (req.domain if req.domain in runner_core.DOMAINS else "algo"),
        "total": int(req.total), "ratios": ratios,
        "seed": req.seed, "concurrency": max(1, min(req.concurrency, 8)),
        "skip_done": req.skip_done,
        "model": req.model.strip(), "protocol": proto,
        "base_url": req.base_url.strip(), "api_key": req.api_key.strip(),
        "max_tokens": max(256, min(int(req.max_tokens), 131072)),
        "temperature": max(0.0, min(float(req.temperature), 2.0)),
        "thinking_level": req.thinking_level if req.thinking_level in ("max", "high", "medium", "low", "off") else "max",
        # 修码/性能题：agent 多轮读文件+本地编译判分，单题时限下限抬到 30 分钟
        "task_timeout": (max(1800.0, min(float(req.task_timeout), 7200))
                         if req.domain in runner_core.DOMAINS and req.domain != "algo"
                         else max(30.0, min(float(req.task_timeout), 7200))),
        "score_timeout": max(3, min(int(req.score_timeout), 300)),
        "system_extra": req.system_extra[:4000],
    }
    run = runner_core.BatchRun(cfg)
    run.on_event = _append_event
    CURRENT = run
    run.start()
    return {"ok": True, "model": cfg["model"], "protocol": proto}


@app.post("/api/stop")
def stop_run():
    if CURRENT and CURRENT.status == "running":
        CURRENT.request_stop()
        return {"ok": True, "stopping": True}
    return {"ok": True, "stopping": False}


@app.get("/api/state")
def state(since: int = 0):
    with _EV_LOCK:
        evs = [e for e in EVENTS if e["seq"] > since]
        last = _SEQ
    snap = CURRENT.snapshot() if CURRENT else {"status": "idle"}
    return {"seq": last, "events": evs, "snapshot": snap}


@app.get("/api/history")
def history():
    return runner_core.scan_history()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()
    url = f"http://{args.host}:{args.port}"
    print(f"题库测试台: {url}  (Ctrl+C 退出)")
    if not args.no_open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
