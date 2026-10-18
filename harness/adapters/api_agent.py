"""API 适配器：OpenAI 兼容接口调用模型。

MVP 脚手架分两种模式：
  algo   单轮：题面 → 代码 → 本地跑测试（LiveCodeBench 型）
  swe    多轮：题面 + 仓库树 → 模型可请求读文件/交 patch（简版 ReAct，后续升级）
计 token、计时。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


# ---------- 思考强度映射 ----------
# 现代模型 API 普遍不支持温度等采样参数，改用"思考强度"。评测默认全部拉到最高，
# 保证测的是模型能力而不是配置。各家参数名不同（映射如下），注入不被端点认识时
# 自动降级去掉参数重试并记 thinking_degraded（防 400 判负）。
#   OpenAI o*/GPT-5*: reasoning_effort = "high"（GPT-5 嵌套语法 reasoning.effort，
#                     flat 参数是兼容入口；不认则降级）
#   Anthropic 协议:   thinking = {"type":"enabled","budget_tokens": N}（GLM 端点实测接受）
#   Gemini 3:         thinking_level（OpenAI 兼容层走 reasoning_effort 映射）
#   DeepSeek R 系:    无可调参数（reasoner 自带思考）→ 降级链自然跳过
EFFORT_BY_LEVEL = {"max": "high", "high": "high", "medium": "medium", "low": "low"}
# max 档不设固定预算：跟随 max_tokens（GLM 端点实测接受 131072；原钳 32000 时代
# D4J 49 题 65536 满额空回复——思考闸门实际由 max_tokens 决定，2026-10-13 放开）
BUDGET_BY_LEVEL = {"max": None, "high": 16000, "medium": 8000, "low": 2048}


def _thinking_kwargs(protocol: str, level: str | None, max_tokens: int) -> dict:
    """把统一思考强度档位映射为该协议的请求参数。level: None/off 不注入。"""
    if not level or level == "off":
        return {}
    if protocol == "anthropic":
        budget = BUDGET_BY_LEVEL[level]
        if budget is None:
            budget = max(1024, max_tokens - 2000)
        else:
            budget = min(budget, max(1024, max_tokens - 1000))
        return {"thinking": {"type": "enabled", "budget_tokens": budget}}
    return {"reasoning_effort": EFFORT_BY_LEVEL[level]}


def _looks_like_param_error(e: Exception) -> bool:
    s = f"{type(e).__name__} {e}".lower()
    return ("400" in str(e) or "badrequest" in s or type(e).__name__ == "TypeError"
            or any(k in s for k in ("reasoning", "thinking", "unexpected", "unknown",
                                    "not support", "invalid_request", "unrecognized")))


def chat(messages: list, model: str, base_url: str | None, api_key: str | None,
         max_tokens: int = 8192, temperature: float = 0.2,
         protocol: str | None = None, timeout: float = 600,
         retries: int = 3, thinking_level: str | None = "max") -> tuple[str, dict]:
    """返回 (回复文本, 用量)。

    protocol: "openai" | "anthropic" | None（自动：glm* 走 anthropic，其余走 openai）。
    anthropic 协议下 base_url/api_key 缺省回落到 ~/.claude/settings.json（GLM 订阅端点）。
    temperature 仅 openai 协议生效（anthropic SDK 该版本无此参数）。
    retries: 限流/瞬时错误退避重试次数（探测协议时传 0 快速失败）。
    thinking_level: 思考强度档位 max/high/medium/low/off（默认 max=评测拉满）；
    注入参数端点不认识时自动去掉重试并在 usage 里记 thinking_degraded。
    """
    if protocol is None:
        protocol = "anthropic" if model.startswith("glm") else "openai"
    if protocol == "anthropic":
        return chat_anthropic(messages, model, max_tokens, temperature,
                              base_url=base_url, api_key=api_key, timeout=timeout,
                              retries=retries, thinking_level=thinking_level)
    from openai import OpenAI

    client = OpenAI(
        base_url=base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        api_key=api_key or os.environ.get("OPENAI_API_KEY")
             or os.environ.get("DEEPSEEK_API_KEY")
             or os.environ.get("ANTHROPIC_AUTH_TOKEN") or "EMPTY",
        timeout=timeout, max_retries=0,
    )
    think_kw = _thinking_kwargs("openai", thinking_level, max_tokens)
    t0 = time.time()
    resp = None
    degraded = False
    last_err = None
    attempt = 0
    while True:
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages, max_tokens=max_tokens,
                temperature=temperature, **think_kw)
            break
        except Exception as e:
            last_err = e
            # 思考参数不被端点认识（400/TypeError 等）：去掉参数降级重试（不计重试次数）
            if think_kw and _looks_like_param_error(e):
                think_kw = {}
                degraded = True
                continue
            name = type(e).__name__
            if name in ("RateLimitError", "APIConnectionError", "APITimeoutError",
                        "InternalServerError", "ServiceUnavailableError") \
                    and attempt < retries:
                wait = (5, 15, 30)[min(attempt, 2)]
                attempt += 1
                print(f"    [{name} 重试 {attempt}/{retries}，等 {wait}s]", flush=True)
                time.sleep(wait)
                continue
            raise
    usage = {
        "prompt_tokens": getattr(resp.usage, "prompt_tokens", 0),
        "completion_tokens": getattr(resp.usage, "completion_tokens", 0),
        "latency_sec": round(time.time() - t0, 1),
    }
    if degraded:
        usage["thinking_degraded"] = True   # 端点不支持思考参数，已按默认强度评测（留痕）
    return resp.choices[0].message.content or "", usage


def chat_anthropic(messages: list, model: str, max_tokens: int = 8192,
                   temperature: float = 0.2, base_url: str | None = None,
                   api_key: str | None = None, timeout: float = 600,
                   retries: int = 3, thinking_level: str | None = "max") -> tuple[str, dict]:
    """Anthropic 协议端点。base_url/api_key 由前端或调用方显式给出；
    缺省回落 ~/.claude/settings.json（GLM 订阅套餐，密钥不落库）。
    glm-5.3 是推理模型，回复含 ThinkingBlock，只拼接 text 块。
    temperature 此 SDK 版本不支持，忽略。thinking_level 经 thinking.budget_tokens
    注入（GLM 端点实测支持）；端点不认时去掉参数降级并记 usage.thinking_degraded。"""
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "tools"))
    from model_config import glm_settings
    from anthropic import Anthropic

    if not base_url or not api_key:
        env = glm_settings()
        base_url = base_url or env.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        api_key = api_key or env.get("ANTHROPIC_AUTH_TOKEN")
    client = Anthropic(base_url=base_url, auth_token=api_key, timeout=timeout, max_retries=0)
    think_kw = _thinking_kwargs("anthropic", thinking_level, max_tokens)
    t0 = time.time()
    resp = None
    degraded = False
    last_err = None
    attempt = 0
    while True:
        try:
            resp = client.messages.create(
                model=model, max_tokens=max_tokens,
                system=next((m["content"] for m in messages if m["role"] == "system"), None),
                messages=[m for m in messages if m["role"] != "system"],
                **think_kw)
            break
        except Exception as e:
            last_err = e
            if think_kw and _looks_like_param_error(e):
                think_kw = {}
                degraded = True
                continue
            name = type(e).__name__
            if ("rate" in name.lower() or "429" in str(e) or "timeout" in name.lower()) and attempt < retries:
                wait = (5, 15, 30)[min(attempt, 2)]
                attempt += 1
                print(f"    [{name} 重试 {attempt}/{retries}，等 {wait}s]", flush=True)
                time.sleep(wait)
                continue
            raise
    if resp is None:
        raise last_err  # type: ignore[misc]
    text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
    usage = {
        "prompt_tokens": getattr(resp.usage, "input_tokens", 0),
        "completion_tokens": getattr(resp.usage, "output_tokens", 0),
        "latency_sec": round(time.time() - t0, 1),
    }
    if degraded:
        usage["thinking_degraded"] = True
    return text, usage


def detect_protocol(model: str, base_url: str, api_key: str) -> tuple[str, dict]:
    """按接口地址自动探测兼容协议（openai / anthropic），返回 (协议, 探测信息)。

    URL 特征决定尝试顺序（含 "anthropic" 先试 anthropic，否则先试 openai），
    第一个调用成功的协议胜出；两个都失败时抛出最后一个异常。
    判定依据是"调用是否报错"，与回复内容无关（推理模型可能只出思考块）。
    """
    order = ["anthropic", "openai"] if "anthropic" in (base_url or "").lower() else ["openai", "anthropic"]
    last_err: Exception | None = None
    for proto in order:
        try:
            text, usage = chat(
                [{"role": "user", "content": "连通性测试：请只回复 OK。"}],
                model, base_url, api_key, max_tokens=2048, protocol=proto, timeout=30, retries=0)
            return proto, {"reply": (text or "").strip()[:120] or "（推理模型未输出文本块，但调用成功）",
                           "usage": usage}
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise last_err  # type: ignore[misc]


def solve_algo(problem_md: str, start_code: str, model: str, base_url=None, api_key=None,
               protocol: str | None = None, max_tokens: int = 8192,
               temperature: float = 0.2, timeout: float = 600,
               system_extra: str = "", thinking_level: str | None = "max") -> dict:
    """LiveCodeBench 型：单轮出代码。system_extra 追加注入到默认系统提示词之后。"""
    system = ("你是竞赛编程选手。只输出一个 Python 代码块（```python ... ```），"
              "必须定义 `def solution(input: str) -> str`，读入整段输入字符串、返回结果字符串。不要解释。")
    if system_extra.strip():
        system += "\n\n" + system_extra.strip()
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"{problem_md}\n\n起始代码（函数签名）：\n```python\n{start_code}\n```"},
    ]
    text, usage = chat(messages, model, base_url, api_key, max_tokens=max_tokens,
                       temperature=temperature, protocol=protocol, timeout=timeout,
                       thinking_level=thinking_level)
    code = ""
    if "```python" in text:
        code = text.split("```python", 1)[1].split("```", 1)[0].strip()
    elif "```" in text:
        code = text.split("```", 1)[1].split("```", 1)[0].strip()
    return {"answer": code, "raw": text, "usage": usage}


# ---------------- SEARCH/REPLACE 修码协议（D4J 等单轮 patch 类） ----------------
# 行级正则：<<<<<<< SEARCH ... ======= ... >>>>>>> REPLACE（容忍标记重复 5~9 个 <）
import re as _re

_SR_BLOCK = _re.compile(
    r"<{5,}[ \t]*SEARCH[^\n]*\r?\n(.*?)\n={5,}[ \t]*\r?\n(.*?)\n>{5,}[ \t]*REPLACE[^\n]*",
    _re.S | _re.I,
)
_SR_HEAD = _re.compile(r"^#{2,4}[ \t]*(?:file|文件)[ \t]*[:：]?[ \t]*(\S+)[ \t]*$", _re.M | _re.I)


def _fuzzy_span(lines: list[str], target: list[str]):
    """在 lines 里找与 target 匹配的连续区间：精确 → rstrip → strip（缩进差异容忍，
    Cli-10 类复制缩进误差）。返回 (start, len, 模式) 或 None。"""
    m = len(target)
    if m == 0:
        return None
    n = len(lines)
    for mode in ("exact", "rstrip", "strip"):
        key = (lambda l: l) if mode == "exact" else (
            (lambda l: l.rstrip()) if mode == "rstrip" else (lambda l: l.strip()))
        tgt = [key(t) for t in target]
        if any(not t and mode == "strip" for t in tgt):
            continue  # strip 模式空行会误配一切空行
        for i in range(n - m + 1):
            if [key(l) for l in lines[i:i + m]] == tgt:
                return i, m, mode
    return None


def apply_search_replace(files: list[tuple[str, str]], answer: str) -> tuple[dict, list, list]:
    """把模型输出的 SEARCH/REPLACE 块应用到对应文件。

    输出格式约定（prompt 已声明）：每个文件先一行 `### file: <path>`，随后跟该文件的若干块。
    返回 (patch_files: {path: new_content}, applied_paths, errors)；块先精确子串匹配，
    失败再行级 rstrip 宽松匹配，再失败记入 errors（该块跳过，不中断其他块）。
    """
    errors: list[str] = []
    # 按 ### file: 头把回答切成文件段；无头的块忽略
    segments: list[tuple[str, str]] = []
    head_positions = [(m.start(), m.group(1)) for m in _SR_HEAD.finditer(answer)]
    for idx, (pos, path) in enumerate(head_positions):
        end = head_positions[idx + 1][0] if idx + 1 < len(head_positions) else len(answer)
        segments.append((path, answer[pos:end]))
    src_map = dict(files)
    patch_files: dict[str, str] = {}
    applied: list[str] = []
    for path, seg in segments:
        if path not in src_map:
            errors.append(f"{path}: unknown_file_path")
            continue
        blocks = _SR_BLOCK.findall(seg)
        if not blocks:
            errors.append(f"{path}: no_blocks_in_section")
            continue
        new_src = src_map[path]
        ok = 0
        for bi, (search, repl) in enumerate(blocks):
            s = search.replace("\r\n", "\n").rstrip("\n")
            r = repl.replace("\r\n", "\n").rstrip("\n")
            if s in new_src:
                new_src = new_src.replace(s, r, 1)
                ok += 1
                continue
            lines = new_src.split("\n")
            span = _fuzzy_span(lines, [l.rstrip() for l in s.split("\n")])
            if span:
                start, n, _mode = span
                lines[start:start + n] = r.split("\n")
                new_src = "\n".join(lines)
                ok += 1
            else:
                errors.append(f"{path}: block{bi + 1}_not_found")
        if ok:
            patch_files[path] = new_src
            applied.append(path)
    return patch_files, applied, errors


def solve_patch(problem_md: str, files: list[tuple[str, str]], trigger_tests: list[str],
                model: str, base_url=None, api_key=None, protocol: str | None = None,
                max_tokens: int = 8192, temperature: float = 0.2, timeout: float = 600,
                system_extra: str = "", thinking_level: str | None = "max") -> dict:
    """D4J 型单轮修码：给 bug 描述 + 触发测试 + 相关源文件全文，模型输出 SEARCH/REPLACE 块。

    files: [(仓库相对路径, 文件全文), ...]（buggy 版）。返回
    {"patch_files": {path: new_content}, "applied", "sr_errors", "raw", "usage"}。
    """
    system = (
        "你是资深软件工程师，任务是修复给出的 bug。只输出修改块，不要解释。格式：\n"
        "### file: <文件路径>\n"
        "<<<<<<< SEARCH\n"
        "<从源文件逐字复制的原代码片段，带足上下文保证在文件中唯一>\n"
        "=======\n"
        "<替换后的代码>\n"
        ">>>>>>> REPLACE\n"
        "可以输出多个文件、每文件多个块（按顺序应用）。只修改修复 bug 必需的代码，"
        "不要改测试文件，不要重构无关代码。"
    )
    if system_extra.strip():
        system += "\n\n" + system_extra.strip()
    parts = [problem_md.strip() or "(无 bug 描述，请依据测试推断)",
             "\n## 当前失败的测试（修复后必须全部通过）\n" +
             "\n".join(f"- {t}" for t in trigger_tests),
             "\n## 相关源文件（当前存在 bug 的版本）\n"]
    for path, content in files:
        parts.append(f"### file: {path}\n```\n{content}\n```\n")
    parts.append("\n请输出 SEARCH/REPLACE 修改块。")
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(parts)},
    ]
    text, usage = chat(messages, model, base_url, api_key, max_tokens=max_tokens,
                       temperature=temperature, protocol=protocol, timeout=timeout,
                       thinking_level=thinking_level)
    patch_files: dict[str, str] = {}
    sr_errors: list[str] = []
    if text.strip():
        patch_files, _, sr_errors = apply_search_replace(files, text)
        if not patch_files and not sr_errors:
            sr_errors.append("answer_contains_no_sr_blocks")
    return {"patch_files": patch_files, "sr_errors": sr_errors, "raw": text, "usage": usage}


def solve_swe(problem_md: str, file_tree: str, read_file, model: str,
              base_url=None, api_key=None, max_turns: int = 6) -> dict:
    """简版多轮修码：模型每轮要么 READ <path>，要么 SUBMIT <unified diff>。read_file(path)->str。"""
    messages = [
        {"role": "system",
         "content": "你是软件工程师。每轮输出一行指令：`READ <文件路径>` 查看文件，或 `SUBMIT` 然后给 unified diff（```diff 代码块）。diff 必须可被 git apply。"},
        {"role": "user", "content": f"{problem_md}\n\n仓库文件树：\n{file_tree}\n\n先定位，再提交修复。"},
    ]
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "latency_sec": 0.0}
    patch = ""
    for _ in range(max_turns):
        text, usage = chat(messages, model, base_url, api_key)
        for k in total_usage:
            total_usage[k] += usage[k]
        first = text.strip().splitlines()[0] if text.strip() else ""
        if first.upper().startswith("READ"):
            path = first.split(maxsplit=1)[1].strip()
            try:
                content = read_file(path)
            except Exception as e:
                content = f"读取失败: {e}"
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": f"```{path}\n{content[:20000]}\n```"})
            continue
        if "```diff" in text:
            patch = text.split("```diff", 1)[1].rsplit("```", 1)[0].strip()
        break
    return {"answer": patch, "raw": text, "usage": total_usage}
