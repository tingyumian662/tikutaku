"""判分：按 verifier_type 分发。

  oj_tests       本地子进程跑生成的代码对 test_list（LiveCodeBench 型，无需 Docker）
  f2p_p2p_tests  生成 predictions jsonl → 官方 swebench harness 判定（对表）
  其他           记 pending，待适配

比较器为归一化等价（2026-10-10 审计后升级）：严格相等 → 空白折叠 → Python 字面量
等价（引号/元组/列表表示法）→ 数值容差 → 逐行等价。动机：GLM 全量审计发现 46 题
纯格式差（`1, 3` vs `1,3`、`"abc"` vs `abc`）被误判 wrong_answer。
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))


def _nums_eq(a: str, b: str) -> bool:
    try:
        fa, fb = float(a), float(b)
    except Exception:
        return False
    return abs(fa - fb) <= 1e-9 * max(1.0, abs(fa), abs(fb))


def _strip_q(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def norm_eq(stdout: str, expected) -> str | None:
    """归一化等价比较。命中返回级别名（exact/whitespace/literal/numeric/line-wise），
    不等价返回 None。expected 为期望字符串。"""
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
            (x == y) or (_strip_q(x) == _strip_q(y)) or _nums_eq(x, y)
            or _nums_eq(_strip_q(x), _strip_q(y)) for x, y in zip(la, lb)):
        return "line-wise"
    return None


def score_oj(answer_code: str, test_list, entry: str = "solution",
             timeout_sec: int = 20) -> dict:
    """支持两种测试格式：
    1) LCB-lite stdin_stdout: [{"input": "...", "expected": "..."}] — 模型写 def solution(input:str)->str
    2) 断言表达式: ["solve(input='..', expected='..')"] — 注入包装校验
    """
    if not answer_code.strip():
        return {"verdict": "compile_error", "passed": 0, "total": len(test_list),
                "note": "empty_answer"}   # 模型没输出代码（推理模型思考链耗尽 token 的典型症状），与语法错区分
    if test_list and isinstance(test_list[0], dict):
        return _score_oj_stdin(answer_code, test_list, timeout_sec)
    wrapper = (
        "def solve(input: str, expected: str) -> bool:\n"
        "    out = solution(input)\n"
        "    return str(out).strip() == str(expected).strip()\n\n"
    )
    harness = answer_code + "\n\n" + wrapper + "if __name__ == '__main__':\n"
    for t in test_list:
        harness += f"    assert {t}\n"
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(harness)
        path = f.name
    t0 = time.time()
    try:
        cp = subprocess.run([sys.executable, path], capture_output=True, text=True,
                            timeout=timeout_sec, encoding="utf-8", errors="replace")
        ok = cp.returncode == 0
        return {"verdict": "pass" if ok else "wrong_answer",
                "passed": len(test_list) if ok else 0, "total": len(test_list),
                "runtime_sec": round(time.time() - t0, 2),
                "err": (cp.stderr or "")[-500:] if not ok else ""}
    except subprocess.TimeoutExpired:
        return {"verdict": "timeout", "passed": 0, "total": len(test_list)}
    finally:
        Path(path).unlink(missing_ok=True)


def _score_oj_stdin(answer_code: str, tests: list[dict], timeout_sec: int) -> dict:
    """逐条喂 input，比对 expected（strip 后比较）。每条单独限时，超时即停。"""
    driver = answer_code + "\n\nimport sys\nif __name__ == '__main__':\n    print(solution(sys.stdin.read()))\n"
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(driver)
        path = f.name
    passed = 0
    first_err = ""
    norm_hit = None
    t0 = time.time()
    try:
        for t in tests:
            try:
                cp = subprocess.run([sys.executable, path], input=t["input"], capture_output=True,
                                    text=True, timeout=timeout_sec, encoding="utf-8", errors="replace")
                lvl = norm_eq(cp.stdout, t["expected"]) if cp.returncode == 0 else None
                if lvl:
                    passed += 1
                    if lvl != "exact" and not norm_hit:
                        norm_hit = lvl
                elif not first_err:
                    first_err = (cp.stderr or f"期望[{str(t['expected'])[:80]}] 实得[{cp.stdout[:80]}]")[-300:]
            except subprocess.TimeoutExpired:
                if not first_err:
                    first_err = "timeout"
                break
        verdict = "pass" if passed == len(tests) else "wrong_answer"
        out = {"verdict": verdict, "passed": passed, "total": len(tests),
               "runtime_sec": round(time.time() - t0, 2),
               "err": first_err if passed < len(tests) else ""}
        if norm_hit:
            out["normalized"] = norm_hit   # 通过但曾命中非严格等价（留痕便于复核）
        return out
    finally:
        Path(path).unlink(missing_ok=True)


def build_predictions(results: list[dict], out: Path) -> int:
    """results: [{bank_id, source_id, patch, model}] → swebench predictions jsonl。"""
    n = 0
    with out.open("w", encoding="utf-8") as f:
        for r in results:
            if not r.get("patch"):
                continue
            f.write(json.dumps({
                "instance_id": r["source_id"],
                "model_patch": r["patch"],
                "model_name_or_path": r.get("model", "unknown"),
            }, ensure_ascii=False) + "\n")
            n += 1
    return n


def run_swebench_eval(pred_file: Path, dataset_name: str, run_id: str,
                      max_workers: int = 2) -> Path | None:
    """跑官方判定，返回报告目录路径。"""
    repo = Path(__file__).parent.parent / "sources" / "repos" / "swebench"
    cmd = ["uv", "run", "--python", "3.11", "--with", "swebench",
           "python", "-m", "swebench.harness.run_evaluation",
           "--predictions_path", str(pred_file),
           "--dataset_name", dataset_name,
           "--run_id", run_id,
           "--max_workers", str(max_workers)]
    cp = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=14400)
    print((cp.stdout or "")[-1500:])
    if cp.returncode != 0:
        print("[swebench eval 失败]", (cp.stderr or "")[-800:])
        return None
    m = re.findall(r"Saved predictions to (.+)", cp.stdout or "")
    return Path(m[-1].strip()).parent if m else None


# ---------------- Defects4J 本地判分（无 Docker） ----------------
# 链路：git 重置工作区 → 写入模型修改的源文件 → defects4j compile → defects4j test F2P。
# 判分红线：区分 apply_fail（修改无法落地）/ compile_error / wrong_answer / pass，
# 并拒绝模型修改测试文件（改测试过关=作弊）。

import os as _os
import threading as _threading

# Windows 重型工作区根：D4J ASCII junction（tb/）、JDK 副本、BIP/Perf checkout、构建 venv。
# 默认 D:/tikutaku（本机布局：项目树在被沙箱打 Low 完整性标记的盘上，重型 exe 必须放干净盘）；
# 复刻者可用环境变量 TIKUTAKU_DATA_ROOT 重定向到任意路径。
DATA_ROOT = Path(_os.environ.get("TIKUTAKU_DATA_ROOT", "D:/tikutaku"))

_D4J_LOCK = _threading.Lock()  # 串行化 D4J 判分：ant/maven 并发写 ~/.m2 会互踩

_D4J_ROOT = Path(__file__).resolve().parent.parent
# D4J 框架一律走 ASCII junction（D:\tikutaku\tb → E:\AI\测试题库）：
# get-deps 的 file:// URL 含中文时 java 解析失败且 ignoreerrors 静默跳过 → 编译缺依赖（2026-10-11 踩坑）
_D4J_SRC = DATA_ROOT / "tb" / "sources" / "repos" / "defects4j"


def _posix(p) -> str:
    """Windows 路径 → cygwin/MSYS POSIX 风格（E:\\x → /e/x）。perl/cygwin 工具链只认这种。"""
    s = str(p)
    if s[1:2] == ":":
        return "/" + s[0].lower() + s[2:].replace("\\", "/")
    return s


def _d4j_env() -> dict:
    env = _os.environ.copy()
    # JDK 必须用 D 盘副本：项目树内的 exe 会被 Low 完整性标记拉低，
    # java 进程写 ~/.maven 等用户目录会被拒（2026-10-11 踩坑）
    jh = DATA_ROOT / "jdk-11.0.32.1+1"
    # cygwin perl 按 ':' 拆 PERL5LIB/JAVA_HOME，必须给 POSIX 风格路径；
    # ant 依赖 D4J 自带的 major/bin/ant（系统无 ant）
    env["JAVA_HOME"] = _posix(jh)
    env["PATH"] = (_posix(jh / "bin") + ":" +
                   _posix(_D4J_SRC / "major" / "bin") + ":" +
                   env.get("PATH", ""))
    env["PERL5LIB"] = _posix(DATA_ROOT / "tb" / "vendor" / "perl5lib")
    # 中文 Windows 的 javac 默认 GBK 读源码，UTF-8 源（德语注释等）会"不可映射字符"编译失败
    env["JAVA_TOOL_OPTIONS"] = "-Dfile.encoding=UTF-8"
    # Mockito 的 chooseDepedencyVersion.sh 需要 bash：/bin/bash 在 Windows 不可解析，
    # 用 perl 同目录的 bash.exe（Git Bash 的 usr/bin 同时有两者），经环境变量交给 ant
    import shutil as _shutil
    _pl = _shutil.which("perl")
    _bash = Path(_pl).with_name("bash.exe") if _pl else Path("bash.exe")
    if _bash.exists():
        env["D4J_BASH"] = _bash.as_posix()
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "core.autocrlf"
    env["GIT_CONFIG_VALUE_0"] = "false"
    return env


def _d4j(args: list[str], workdir: Path, timeout: float) -> subprocess.CompletedProcess:
    env = _d4j_env()
    env["D4J_HOME"] = _posix(_D4J_SRC)
    args = [_posix(a) if a.startswith("E:") or a.startswith("e:") else a for a in args]
    cmd = ["perl", _posix(_D4J_SRC / "framework" / "bin" / "defects4j")] + args
    return subprocess.run(cmd, cwd=str(workdir), env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)


_TEST_PATH_HINTS = ("/test/", "/tests/", "tests/", "/test_", "_test.py",
                    "Test.java", "Tests.java", "IT.java")


def _is_test_path(p: str) -> bool:
    return any(h in p.replace("\\", "/") for h in _TEST_PATH_HINTS)


def score_d4j(workdir: Path, patch_files: dict[str, str], trigger_tests: list[str],
              timeout_sec: int = 1800, run_p2p: bool = False,
              allow_new_files: bool = False) -> dict:
    """Defects4J 判分。workdir = buggy 版工作区（会被 git 重置复用）。

    patch_files: {仓库相对路径: 新全文}（来自 SEARCH/REPLACE 应用结果）
    trigger_tests: F2P（org.pkg.Class::method）；同类合并为类级一次跑，失败类=未修复。
    allow_new_files: 金标审计用——允许写入不存在的新文件（金标可含新增源文件，
    如 Jsoup-71 的 PseudoTextElement.java）；模型流保持 False 挡幻觉路径。
    """
    t0 = time.time()
    note_bits: list[str] = []

    def out(verdict: str, **kw) -> dict:
        d = {"verdict": verdict, "sec": round(time.time() - t0, 1)}
        d.update(kw)
        if note_bits:
            d["note"] = "; ".join(note_bits)
        return d

    if not trigger_tests:
        return out("error:no_f2p")
    for p in patch_files:
        if _is_test_path(p):
            note_bits.append(f"test_file_rejected:{p}")
    patch_files = {p: c for p, c in patch_files.items() if not _is_test_path(p)}
    if not patch_files:
        return out("apply_fail", reason=note_bits and note_bits[0] or "no_source_changes")

    with _D4J_LOCK:
        # 1) 重置到 buggy 基线（金标验证与模型判分共用工作区）；build.xml 重置后需重新修 URL
        for a in (["checkout", "."], ["clean", "-fdq"]):
            subprocess.run(["git"] + a, cwd=str(workdir), capture_output=True, timeout=120)
        _fix_d4j_build_xml(workdir)
        # 2) 写入修改
        try:
            for p, content in patch_files.items():
                fp = workdir / p
                if not fp.resolve().is_relative_to(workdir.resolve()):
                    return out("apply_fail", reason=f"path_escape:{p}")
                if not fp.exists():
                    if not allow_new_files:
                        return out("apply_fail", reason=f"no_such_file:{p}")
                    fp.parent.mkdir(parents=True, exist_ok=True)
                # latin-1 单字节无损往返（与 run.py 读取域一致），newline="\n" 防 CRLF 破坏补丁
                fp.write_text(content, encoding="latin-1", newline="\n")
        except OSError as e:
            return out("apply_fail", reason=f"write_error:{e}")

        # 3) 编译（cygwin perl 的输出在 stderr；成功以 rc==0 为准）
        try:
            cp = _d4j(["compile", "-w", str(workdir)], workdir, timeout_sec)
        except subprocess.TimeoutExpired:
            return out("timeout", stage="compile")
        if cp.returncode != 0:
            note_bits.append("compile_log_tail:" + ((cp.stdout or "") + (cp.stderr or ""))[-300:].replace("\n", " "))
            return out("compile_error")

        # 4) F2P（同类合并；失败输出形如 "Failing tests: N\n  - org.x.ClassY"）
        classes: dict[str, set[str]] = {}
        for t in trigger_tests:
            cls, _, mth = t.partition("::")
            classes.setdefault(cls, set()).add(mth)
        failing: list[str] = []
        for cls in classes:
            try:
                tp = _d4j(["test", "-w", str(workdir), "-t", cls], workdir, timeout_sec)
            except subprocess.TimeoutExpired:
                note_bits.append(f"test_timeout:{cls}")
                failing.append(cls)
                continue
            body = ((tp.stdout or "") + "\n" + (tp.stderr or ""))
            m = re.search(r"Failing tests:\s*(\d+)\n((?:\s+-\s.+\n?)+)", body)
            if m and int(m.group(1)) > 0:
                failing += [l.strip().lstrip("- ").strip() for l in m.group(2).splitlines()]
        if failing:
            return out("wrong_answer", failing=failing[:10])
        return out("pass", f2p_only=(not run_p2p))


D4J_TASKS_ROOT = Path("E:/AI/d4j_work/tasks")  # 必须纯 ASCII（ant 在中文路径下会失败）


def d4j_checkout(pid: str, vid_num: str, bank_id: str, timeout: float = 600) -> Path | None:
    """按需物化 buggy 工作区（幂等：已存在直接返回）。vid_num 是不带 b/f 后缀的数字。"""
    with _D4J_LOCK:  # checkout 与判分共用 git bundle，串行避免互踩
        workdir = D4J_TASKS_ROOT / bank_id
        if (workdir / ".defects4j.config").exists():
            _fix_d4j_build_xml(workdir)
            return workdir
        workdir.mkdir(parents=True, exist_ok=True)
        env = _d4j_env()
        env["D4J_HOME"] = _posix(_D4J_SRC)
        cmd = ["perl", _posix(_D4J_SRC / "framework" / "bin" / "defects4j"),
               "checkout", "-p", pid, "-v", f"{vid_num}b", "-w", _posix(workdir)]
        try:
            cp = subprocess.run(cmd, env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        if (workdir / ".defects4j.config").exists():
            _fix_d4j_build_xml(workdir)
            return workdir
        return None


def _fix_d4j_build_xml(workdir: Path) -> None:
    r"""ant build.xml 的 file:///x/ URL 缺盘符冒号，Windows java 解析失败且 get ignoreerrors
    静默跳过 → 编译缺依赖（2026-10-11 踩坑）。file:///d/ → file:///D:/，幂等。
    gradle wrapper 同病：file:/d/（properties 里冒号转义为 file\:/）在 Windows 解析到
    错误盘符 → gradlew.bat 拿不到本地发行版（Mockito 2026-10-15）。"""
    bx = workdir / "build.xml"
    if bx.exists():
        t = bx.read_text(encoding="utf-8", errors="surrogateescape")
        t2 = re.sub(r"file:///([a-zA-Z])/", lambda m: f"file:///{m.group(1).upper()}:/", t)
        if t2 != t:
            bx.write_text(t2, encoding="utf-8", errors="surrogateescape", newline="\n")
    # .defects4j.config 若为 CRLF：perl 读 pid 带上 \r → "Can't locate Project/Mockito\r.pm"
    # （D4J-000791 实锤）。必须字节级判断——read_text 的 universal newlines 会把
    # CRLF 翻译成 \n，'\r' in t 永远为假，写回被静默跳过
    cfg = workdir / ".defects4j.config"
    if cfg.exists():
        raw = cfg.read_bytes()
        if b"\r" in raw:
            cfg.write_bytes(raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n"))
    gw = workdir / "gradle" / "wrapper" / "gradle-wrapper.properties"
    if gw.exists():
        t = gw.read_text(encoding="utf-8", errors="surrogateescape")
        t2 = re.sub(r"file\\:/([dDeE])/", lambda m: f"file\\:///{m.group(1).upper()}:/", t)
        t2 = re.sub(r"(?<!:)file:/([dDeE])/", lambda m: f"file:///{m.group(1).upper()}:/", t2)
        if t2 != t:
            gw.write_text(t2, encoding="utf-8", errors="surrogateescape", newline="\n")
    # gradle 构建脚本（Mockito 新版）：D4J 补丁写的是 POSIX 根路径 "/d/tikutaku/..."，
    # Windows gradle 解析成当前盘符相对路径 → 本地 deps 仓库找不到 → 落到已死的 jcenter
    for name in ("build.gradle", "settings.gradle"):
        gg = workdir / name
        if gg.exists():
            t = gg.read_text(encoding="utf-8", errors="surrogateescape")
            t2 = re.sub(r'"/([dDeE])/', lambda m: f'"{m.group(1).upper()}:/', t)
            # osgify 是发布产物的 OSGi 打包步骤（doLast 里调外部 ant/ant.bat/antCommand
            # ——各 commit 年代写法不同），编译/测试用不到；gradle 的 Windows 进程
            # PATH 解析不了 cygwin ant → 换成 cmd 空操作
            t2 = re.sub(r"commandLine\s+[^,]+,\s*'-f',\s*'build-ant\.xml'[^\n]*",
                        "commandLine 'cmd', '/c', 'exit /b 0'", t2)
            if t2 != t:
                gg.write_text(t2, encoding="utf-8", errors="surrogateescape", newline="\n")


def d4j_gold_paths(bank_id: str) -> list[str]:
    """金标 patch 涉及的源码文件路径（仅路径，绝不含金标内容；测试/config 文件剔除）。"""
    from seal_gold import unseal
    patch = unseal(bank_id)
    if not patch:
        return []
    heads = re.findall(r"diff --git a/(\S+) b/(\S+)", patch)
    out = []
    for a, b in heads:
        p = a if a == b else b
        if p == ".defects4j.config" or _is_test_path(p):
            continue
        if p not in out:
            out.append(p)
    return out


# ---------------- BugsInPy 本地判分（WSL 路线，2026-10-11 建） ----------------
# pandas/matplotlib 等 C 扩展项目在 Windows 编不动，判分子进程走 WSL（tikutaku 发行版，
# gcc + uv + python3.8 已装）。物化用 codeload tarball（校园网实测秒级），无 git 历史。
# 注意：Windows 侧 python 必须是 Medium 完整性（D 盘 venv）才能调 wsl.exe。

BIP_TASKS_ROOT = DATA_ROOT / "bip_tasks"
BIP_FIXED_ROOT = DATA_ROOT / "bip_fixed"
BIP_VENV_DIR = "/root/bip_venvs"          # WSL 内 ext4，跨文件系统跑 venv 慢
BIP_PY = "/root/.local/share/uv/python/cpython-3.8-linux-x86_64-gnu/bin/python3.8"

_WSL = ["wsl", "-d", "tikutaku", "--", "bash", "-c"]


def _wsl_path(p) -> str:
    r"""D:\x\y → /mnt/d/x/y；相对路径先锚定项目根（中文路径 WSL p9 挂载支持）。"""
    pp = Path(p)
    if not pp.is_absolute():
        pp = _D4J_ROOT / pp
    s = str(pp).replace("\\", "/")
    if s[1:2] == ":":
        return "/mnt/" + s[0].lower() + s[2:]
    return s


def _wsl_run(cmd: str, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(_WSL + [cmd], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def bip_checkout(bank_id: str, repo_url: str, buggy_commit: str, timeout: float = 300) -> Path | None:
    """codeload tarball 物化 buggy 版（幂等）。返回 Windows 侧 workdir。"""
    workdir = BIP_TASKS_ROOT / bank_id
    if workdir.exists() and any(workdir.iterdir()):
        return workdir
    workdir.mkdir(parents=True, exist_ok=True)
    repo = repo_url.rstrip("/").split("github.com/")[1]
    url = f"https://codeload.github.com/{repo}/tar.gz/{buggy_commit}"
    tar = workdir.parent / f"{bank_id}.tar.gz"
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "tikutaku"})
        with urllib.request.urlopen(req, timeout=timeout) as r, open(tar, "wb") as f:
            f.write(r.read())
    except Exception:
        return None
    import tarfile
    try:
        with tarfile.open(tar) as tf:
            root = tf.getmembers()[0].name.split("/")[0]
        # WSL tar 抽取：①符号链接要原样落地（ansible setup.py 靠 bin/ 链接判断 git 结构，
        # Windows tarfile 连 data/tar filter 都会剥掉或因无符号链接权限失败）
        # ②躲开 Windows 260 字符路径限制（ansible placebo 深路径实测踩中）
        cp = _wsl_run(f"tar -xzf {_wsl_path(tar)} -C {_wsl_path(workdir.parent)}", 600)
        if cp.returncode != 0:
            raise RuntimeError((cp.stderr or cp.stdout or "wsl tar failed")[-200:])
        src = workdir.parent / root
        if workdir.exists():
            import shutil as _sh
            _sh.rmtree(workdir)  # mkdir 预建的空壳；rename 不允许目标存在
        src.rename(workdir)
    except Exception:
        return None
    finally:
        tar.unlink(missing_ok=True)
    return workdir if workdir.exists() else None


def bip_prepare_test(workdir: Path, bank_id: str, repo_url: str, fixed_commit: str,
                     bug_dir: Path) -> bool:
    """用 fixed 版的测试文件覆盖 buggy 工作区（BIP 官方流程：F2P 测试方法只在 fixed 版存在）。

    fixed tarball 缓存在 BIP_FIXED_ROOT/<bank_id>，不存在则现场拉取。bug_dir 的 bug.info
    提供 test_file 路径。金标涉及测试文件时同样从 fixed 版取（判分红线：模型不许改测试）。
    """
    import re as _re
    import tarfile
    import urllib.request

    fixed = BIP_FIXED_ROOT / bank_id
    if not fixed.exists():
        repo = repo_url.rstrip("/").split("github.com/")[1]
        url = f"https://codeload.github.com/{repo}/tar.gz/{fixed_commit}"
        tar = fixed.parent / f"{bank_id}.tar.gz"
        fixed.parent.mkdir(parents=True, exist_ok=True)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "tikutaku"})
            with urllib.request.urlopen(req, timeout=180) as r, open(tar, "wb") as f:
                f.write(r.read())
            with tarfile.open(tar) as tf:
                root = tf.getmembers()[0].name.split("/")[0]
                tf.extractall(fixed.parent, filter="tar")
            import shutil as _sh
            if fixed.exists():
                _sh.rmtree(fixed)
            (fixed.parent / root).rename(fixed)
        except Exception:
            return False
        finally:
            tar.unlink(missing_ok=True)
    info_path = bug_dir / "bug.info"
    info = ""
    if info_path.exists():
        raw = info_path.read_bytes()
        # BIP 的 bug.info 编码不统一：UTF-16 LE（带 BOM）或 UTF-8，按前缀自适应
        info = raw.decode("utf-16") if raw[:2] == b"\xff\xfe" else raw.decode("utf-8", errors="replace")
    m = _re.search(r'test_file="([^"]+)"', info)
    if not m:
        return False
    import shutil as _sh
    copied = False
    # 测试基准 = fixed 版整目录同步（bug.info 的 test_file 常列不全辅助文件，
    # 如 PySnooper 的 tests/mini_toolbox.py 只在 fixed 存在）；源码目录不动
    test_dirs = sorted({(fixed / tf.strip()).parent for tf in m.group(1).split(";") if tf.strip()})
    for tdir in test_dirs:
        if not (tdir.exists() and tdir.name):
            continue
        dst_dir = workdir / tdir.relative_to(fixed)
        if not dst_dir.exists():
            continue  # 测试目录在 buggy 也应存在，防误拷未知目录
        _sh.copytree(tdir, dst_dir, dirs_exist_ok=True)
        copied = True
    return copied


def bip_venv(project: str, requirements_txt: Path, rebuild: bool = False,
             bank_id: str = "") -> str | None:
    """WSL 内每项目一个 py3.8 venv（幂等，靠 .tikutaku_fixed 标记判定完成态）。

    bank_id 非空时先查 tools/bip_venv_alias.json 的按题路由（如 fastapi 跨
    pydantic 0.x/1.x 时代，单 venv 装不下所有 pin——同 repo 分裂多 venv）。
    """
    if bank_id:
        try:
            _ap = Path(__file__).resolve().parent.parent / "tools" / "bip_venv_alias.json"
            alias = json.loads(_ap.read_text(encoding="utf-8"))
            project = alias.get(bank_id, project)
        except Exception:
            pass
    venv = f"{BIP_VENV_DIR}/{project}"
    probe = _wsl_run(f"[ -f {venv}/.tikutaku_fixed ] && echo OK", 30)
    if "OK" in (probe.stdout or "") and not rebuild:
        return venv
    if rebuild or "OK" not in _wsl_run(f"[ -x {venv}/bin/python ] && echo OK", 30).stdout:
        _wsl_run(f'export PATH="/root/.local/bin:$PATH" && uv venv {venv} --python 3.8 --seed '
                 f'&& rm -f {venv}/.tikutaku_fixed', 240)
    # BIP 是 2019-2020 Windows pip freeze 生态：混有 pywin32 等 Windows 专属包（Linux 无此发行版
    # 会让整次安装失败），且老 requirements 会把 typing-extensions 降级打坏 exceptiongroup——
    # 依赖装完后强制恢复。clone 类 editable 落 WSL ext4（/mnt 上又慢又触发 WSL 性能告警）。
    _ws = "https://pypi.tuna.tsinghua.edu.cn/simple"
    # 先降 setuptools<71：老 sdist（tornado 5.0 等）在新 setuptools 下 metadata 构建崩（坑④）
    _wsl_run(f'export PATH="{venv}/bin:$PATH" && python -m pip install "setuptools<71" wheel -q '
             f"-i {_ws} --no-warn-script-location", 300)
    if requirements_txt and requirements_txt.exists():
        # 编码在 python 侧判定（iconv 对 UTF-8 输入 LE 转换会"成功"产出乱码）：按 BOM 分流
        raw = requirements_txt.read_bytes()
        text = (raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff")
                else raw.decode("utf-8", errors="replace"))
        lines = [l for l in text.splitlines()
                 if l.strip() and not l.lower().startswith(
                     ("pywin32", "win32", "pywinpty", "pythonwin", "pypiwin32"))]
        req_local = BIP_TASKS_ROOT / "_req.txt"
        req_local.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        wr = _wsl_path(req_local)
        # 源里不存在的死包（requests-async==0.5.0 等）：整体失败后退化为逐包容错安装
        inst = (
            f'python -m pip install -r {wr} -q -i {_ws} --no-warn-script-location || '
            f'while IFS= read -r l; do [ -z "$l" ] && continue; '
            f'python -m pip install "$l" -q -i {_ws} --no-warn-script-location '
            f'|| echo "SKIP:$l" >> /root/bip_skipped.txt; done < {wr}'
        )
        script = f'#!/bin/bash\ncd /root\nexport PATH="{venv}/bin:$PATH"\n{inst}\n'
        sf = BIP_TASKS_ROOT / f"_venv_{uuid.uuid4().hex[:8]}.sh"
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text(script, encoding="utf-8", newline="\n")
        _wsl_run(f"bash {_wsl_path(sf)}", 2400)
    # 修复段：老生态钉版会被 requirements 打坏，统一恢复（setuptools≥75 不支持 py3.8，会 vendored
    # typeguard 崩；typing-extensions 升回 4.x 供 exceptiongroup 使用）
    _wsl_run(f'cd /root && export PATH="{venv}/bin:$PATH" && python -m pip install pytest nose '
             f'"typing-extensions>=4.6" "setuptools<71" wheel -q -i {_ws} --no-warn-script-location '
             f'&& touch {venv}/.tikutaku_fixed', 900)
    ok = _wsl_run(f"[ -x {venv}/bin/python ] && echo OK", 30)
    return venv if "OK" in (ok.stdout or "") else None


def score_bip(workdir: Path, patch_files: dict[str, str], test_nodes: list[str],
              venv: str, timeout_sec: int = 900, cwd_setup: str = "") -> dict:
    """BIP 判分：写回修改 → WSL venv 跑 pytest 节点 → rc==0 全过 = pass。

    test_nodes: pytest 节点（path::Class::method）；unittest 格式（module.Class.method）
    自动转 pytest 可跑形式（path 换算不出的用 python -m unittest 跑）。
    """
    t0 = time.time()

    def out(verdict: str, **kw) -> dict:
        d = {"verdict": verdict, "sec": round(time.time() - t0, 1)}
        d.update(kw)
        return d

    if not test_nodes:
        return out("error:no_f2p")
    for p in patch_files:
        if _is_test_path(p):
            return out("apply_fail", reason=f"test_file_rejected:{p}")
    if not patch_files:
        return out("apply_fail", reason="no_source_changes")
    try:
        for p, content in patch_files.items():
            fp = workdir / p
            if not fp.resolve().is_relative_to(workdir.resolve()):
                return out("apply_fail", reason=f"path_escape:{p}")
            if not fp.exists():
                return out("apply_fail", reason=f"no_such_file:{p}")
            fp.write_text(content, encoding="latin-1", newline="\n")
    except OSError as e:
        return out("apply_fail", reason=f"write_error:{e}")

    wd = _wsl_path(workdir)
    # unittest 的 dotted 节点（module.Class.method）转 pytest 路径形式，统一 pytest 执行
    def _to_pytest(n: str) -> str:
        if "::" in n or n.endswith(".py"):
            return n
        parts = n.split(".")
        if len(parts) >= 3:
            return "/".join(parts[:-2]) + ".py::" + "::".join(parts[-2:])
        return n
    nodes = " ".join(f"'{_to_pytest(n)}'" for n in test_nodes)
    if not nodes:
        return out("error:no_f2p")
    # venv 的 bin 注入 PATH（带引号：WSL 继承的 Windows PATH 含空格路径）；
    # BIP 的 setup.sh 用裸 pip3/python，必须在 venv 里执行。
    # setup.sh 的 --force 会让 pandas 等 C 扩展项目每次判分全量重编（15 分钟+），
    # 剥掉走增量：判分前 git checkout 重置后 mtime 变化的源文件会触发对应模块重编，正确性不变
    # setup.sh 的职责（装包+编译）已被 venv 依赖 + PYTHONPATH 注入 + 懒 build_ext 替代；
    # DrvFs 上 setup.py install 拷万级文件纯 I/O 开销（ansible 单题 20+ 分钟）。
    # TIKU_SKIP_SETUP=1 时跳过（pandas 已标定流不受影响：批命令不设此变量）。
    import os as _os
    if _os.environ.get("TIKU_SKIP_SETUP") == "1":
        cwd_setup = ""
    cwd_setup = cwd_setup.replace(" --force", " ") if cwd_setup else cwd_setup
    # C 扩展项目（pandas 等 2019 代码 + Cython 0.29 产物）在 gcc13 下 -Werror=array-bounds 误报，用 gcc-10
    # PYTHONPATH 注入：共享 venv 的 egg-link（setup.sh 的 pip install -e）会指向某个旧工作区，
    # 强制导入解析到当前判分工作区（lib/ src/ 根三种布局通吃，不存在即被忽略）
    # 惰性 build_ext：无 setup.sh 的题（matplotlib 29/30）扩展从未编译 → ft2font ImportError；
    # 有标记文件则跳过（幂等），失败不打标记下次重试
    setup = (f'cd {wd} && export PATH="{venv}/bin:$PATH" && '
             'export PYTHONPATH="$PWD:$PWD/lib:$PWD/src:$PYTHONPATH"; '
             'export MPLBACKEND=Agg; '
             'export PYTHONWARNINGS=ignore::DeprecationWarning; '
             'command -v gcc-10 >/dev/null && export CC=gcc-10; '
             'if [ -f setup.py ] && [ ! -f .tikutaku_built ] && [ -z "$TIKU_SKIP_LAZY_BUILD" ]; then '
             'python setup.py build_ext --inplace -j 0 >/dev/null 2>&1 && touch .tikutaku_built; fi; '
             + (f"{cwd_setup} && " if cwd_setup else ""))
    try:
        # wsl.exe 参数层会吞 $?/${...}（实测 false;echo $? 得 0），判分链一律落脚本文件执行，
        # pytest 的退出码经 bash exit 透传到 subprocess.returncode
        script = f"""#!/bin/bash
{setup.strip()}
O=/tmp/bip_out_$$.txt
{venv}/bin/python -m pytest -q -x -W ignore::DeprecationWarning -W ignore::PendingDeprecationWarning {nodes} > $O 2>&1
rc=$?
tail -6 $O
rm -f $O
exit $rc
"""
        sf = BIP_TASKS_ROOT / f"_judge_{uuid.uuid4().hex[:8]}.sh"
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text(script, encoding="utf-8", newline="\n")
        cp = _wsl_run(f"bash {_wsl_path(sf)}", timeout_sec)
        body = (cp.stdout or "") + (cp.stderr or "")
        if cp.returncode == 0:
            return out("pass")
        tail = body[-300:].replace("\n", " ")
        if "ModuleNotFoundError" in tail or "ImportError" in tail or "No module named" in tail:
            return out("error:env_fail", tail=tail[:200])
        if cp.returncode in (2, 3, 4):  # 收集/内部错误 ≠ 模型修错，别计 wrong_answer
            # 例外：pytest-xdist 下 -x 首个失败抛 dsession.Interrupted（rc=2），
            # 这是"已有测试失败被拦停"，语义是模型没修好，不是管线错误
            if "Interrupted" in tail or "stopping after" in tail:
                return out("wrong_answer", tail=tail[:200])
            return out("error:test_error", tail=tail[:200])
        return out("wrong_answer", tail=tail[:200])
    except subprocess.TimeoutExpired:
        return out("timeout", stage="test")
