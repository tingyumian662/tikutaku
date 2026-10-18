"""判分噪声审计：只用已落盘的做题痕迹（answer/raw/score），不调任何 API。

对 glm-5.3 全量记录做失败归因：
  compile_error(空答案) → raw 也空 = 输出管线失败；raw 有货没围栏 = 提取失败
  wrong_answer          → 用归一化比较器本地重跑存的代码，能翻案的 = 格式噪声
归一化分级：空白折叠 → Python 字面量等价（引号/元组/列表） → 数值容差 → 逐行等价。
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))


def collect(model: str = "glm-5.3") -> dict[str, dict]:
    recs: dict[str, dict] = {}
    for f in sorted(ROOT.joinpath("results").glob("run-*/results.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("model") == model and r.get("bank_id"):
                recs[r["bank_id"]] = r   # 新 run 覆盖旧
    return recs


def load_tests() -> dict[str, list]:
    import pandas as pd
    df = pd.read_parquet(ROOT / "bank" / "index.parquet", columns=["bank_id", "fail_to_pass"])
    out = {}
    for bid, ftp in zip(df["bank_id"], df["fail_to_pass"]):
        try:
            out[bid] = json.loads(ftp)
        except Exception:
            pass
    return out


# ---------- 归一化比较器（分级） ----------
def _nums_eq(a: str, b: str) -> bool:
    try:
        fa, fb = float(a), float(b)
    except Exception:
        return False
    return abs(fa - fb) <= 1e-9 * max(1.0, abs(fa), abs(fb))


def norm_eq(stdout: str, expected: str) -> str | None:
    """返回命中的归一化级别（说明翻案原因），未命中返回 None。"""
    a, b = stdout.strip(), str(expected).strip()
    if a == b:
        return "exact"
    if "".join(a.split()) == "".join(b.split()):
        return "whitespace"
    try:
        if ast.literal_eval(a) == ast.literal_eval(b):
            return "literal"
    except Exception:
        pass
    if _nums_eq(a, b):
        return "numeric"
    la, lb = a.splitlines(), b.splitlines()
    if len(la) == len(lb) and la and all(
        (x == y) or (_strip_q(x) == _strip_q(y)) or _nums_eq(x, y) or _nums_eq(_strip_q(x), _strip_q(y))
        for x, y in zip(la, lb)):
        return "line-wise"
    return None


def _strip_q(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def rerun(answer_code: str, tests: list[dict], timeout_sec: int = 10) -> tuple[str, str | None]:
    """重跑存的代码。返回 (verdict, 翻案级别说明)。"""
    driver = answer_code + "\n\nimport sys\nif __name__ == '__main__':\n    print(solution(sys.stdin.read()))\n"
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(driver)
        path = f.name
    hit_level = None
    strict_all = True
    try:
        for t in tests:
            try:
                cp = subprocess.run([sys.executable, path], input=t["input"], capture_output=True,
                                    text=True, timeout=timeout_sec, encoding="utf-8", errors="replace")
            except subprocess.TimeoutExpired:
                return "timeout", None
            if cp.returncode != 0:
                return "compile_error", None
            lvl = norm_eq(cp.stdout, str(t["expected"]))
            if lvl is None:
                return "wrong_answer", None
            if lvl != "exact":
                strict_all = False
                hit_level = hit_level or lvl
        return ("pass_strict" if strict_all else "pass_normalized"), hit_level
    finally:
        Path(path).unlink(missing_ok=True)


def main() -> None:
    t0 = time.time()
    recs = collect()
    tests = load_tests()
    print(f"记录 {len(recs)} 题 | 审计开始")

    verdicts = Counter()
    flippable: list[tuple[str, str]] = []      # (bank_id, 原因)
    empty_answer_raw: Counter = Counter()
    samples: dict[str, list] = {}

    for bid, r in recs.items():
        v = (r.get("score") or {}).get("verdict", "?")
        verdicts[v] += 1
        if v == "pass":
            continue
        answer = r.get("answer") or ""
        raw = r.get("raw") or ""
        if v == "compile_error":
            if not answer.strip():
                if not raw.strip():
                    # 思考链耗尽 max_tokens / API 空回复
                    verdicts["归因:空回复(raw空)"] += 1
                    samples.setdefault("空回复", []).append(bid)
                elif "```" in raw:
                    verdicts["归因:有围栏但提取空"] += 1   # 理论不应发生
                else:
                    verdicts["归因:无代码围栏"] += 1
                    samples.setdefault("无围栏", []).append(bid)
                    empty_answer_raw["no_fence"] += 1
            else:
                verdicts["归因:真语法错"] += 1
            continue
        if v in ("wrong_answer", "timeout") and bid in tests and answer.strip():
            nv, lvl = rerun(answer, tests[bid])
            if nv in ("pass_strict", "pass_normalized"):
                # 注意：wrong_answer 却 strict pass = 当年偶发（判分环境差异），也算翻案
                verdicts[f"翻案:{nv}"] += 1
                flippable.append((bid, lvl or "exact(now)"))
                samples.setdefault("翻案", []).append(f"{bid}({lvl})")
            elif nv == "timeout":
                verdicts["维持:超时"] += 1
            elif nv == "compile_error":
                verdicts["翻案失败:重跑编译错"] += 1
            else:
                verdicts["维持:真错"] += 1
        elif v == "timeout":
            verdicts["维持:超时(无代码)"] += 1

    print("\n=== 归因统计 ===")
    for k, c in verdicts.most_common():
        print(f"  {k:<28} {c}")
    total = len(recs)
    fl = len(flippable)
    print(f"\n总题 {total} | 原 pass {verdicts.get('pass',0)} "
          f"({verdicts.get('pass',0)/total:.1%})")
    new_pass = verdicts.get("pass", 0) + fl
    print(f"审计后可翻案 {fl} 题 → 新 pass {new_pass} ({new_pass/total:.1%})")
    print(f"空答案(compile_error) {verdicts.get('compile_error',0)} 题中："
          f"raw空 {sum(c for k,c in empty_answer_raw.items())} 追溯见上")
    if flippable[:10]:
        print("\n翻案样本:", ", ".join(b for b, _ in flippable[:10]))
    for k, v in samples.items():
        print(f"{k} 样本: {v[:6]}")
    print(f"\n耗时 {time.time()-t0:.0f}s")
    out = ROOT / "logs" / "scoring_noise_audit.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({
        "verdicts": dict(verdicts),
        "flippable": flippable,
        "empty_reply": samples.get("空回复", []),       # 全量：raw 为空的题（待重测）
        "samples": {k: v[:50] for k, v in samples.items()},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"明细已存 {out}")


if __name__ == "__main__":
    main()
