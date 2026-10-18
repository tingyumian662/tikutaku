"""SWE-Perf 效率判分器（WSL 路线，2026-10-12 建成）。

链路：codeload 物化 base_commit → 应用 test_patch → 模型/金标 patch →
跑 efficiency_test N 次计时（pytest 内部时长，取中位）→ 与金标中位数对比。

计时要点（噪声控制）：预热 1 次 + N 次取中位；对比必须同环境同窗口（金标与模型
patch 在同一 workdir 交替跑不行——必须先后完整批次）；用 pytest 报告时长而非 wall
（WSL 启动噪声 ~3s）。

verdict：pass（提速达标）/ insufficient_speedup / wrong_answer（测试挂）/
apply_fail / error:env_fail
"""
from __future__ import annotations

import json
import re
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "tools"))

import scoring  # noqa: E402

PERF_TASKS_ROOT = scoring.DATA_ROOT / "perf_tasks"
PERF_VENV_DIR = "/root/perf_venvs"
PY38 = "/root/.local/share/uv/python/cpython-3.8-linux-x86_64-gnu/bin/python3.8"
WS = "https://pypi.tuna.tsinghua.edu.cn/simple"

# repo → PyPI 包（version 拼 PyPI 版本；效率测试跑源码目录，装包只为依赖齐全）
PERF_PKGS = {
    "sympy": "sympy=={v}", "xarray": "xarray=={v}", "scikit-learn": "scikit-learn=={v}",
    "astropy": "astropy=={v}", "sphinx": "Sphinx=={v}", "seaborn": "seaborn=={v}",
    "matplotlib": "matplotlib=={v}", "pylint": "pylint=={v}", "requests": "requests=={v}",
}

# sklearn 0.21 bundled cloudpickle 按 py3.7 签名（15 参）调 types.CodeType；py3.8
# 新增第 2 位 posonlyargcount（16 参）。sklearn/utils/testing.py 硬 import
# externals.joblib（SKLEARN_SITE_JOBLIB 也救不了），只能垫。直接换 types.CodeType 会
# 破坏 numpy 的 isinstance(x, types.CodeType)——必须只在 cloudpickle 模块 exec 窗口内替换
SK_CODETYPE_SHIM = '''\
import importlib.abc, sys, types as _t

_ORIG_CT = _t.CodeType

def _compat_ct(*args):
    if len(args) == 15:
        args = (args[0], 0) + args[1:]
    return _ORIG_CT(*args)

class _LoaderWrap:
    def __init__(self, loader):
        self._loader = loader
    def create_module(self, spec):
        return self._loader.create_module(spec)
    def exec_module(self, module):
        _t.CodeType = _compat_ct
        try:
            self._loader.exec_module(module)
        finally:
            _t.CodeType = _ORIG_CT
    def __getattr__(self, name):
        return getattr(self._loader, name)

class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.split(".")[-1].startswith("cloudpickle"):
            return None
        for f in sys.meta_path:
            if f is self:
                continue
            spec = f.find_spec(fullname, path, target)
            if spec is not None and spec.loader is not None:
                spec.loader = _LoaderWrap(spec.loader)
                return spec
        return None

sys.meta_path.insert(0, _Finder())
'''


def perf_venv(project: str, version: str, install_cmd: str = "", spec_workdir: Path | None = None, spec_repo: str = "") -> str | None:
    """每 repo×major-version 一个 WSL venv（幂等，标记文件判定）。

    Python 版本按项目支持矩阵选（xarray 2023+ 要 3.9+，用 3.10；其余 3.8）。
    """
    # sklearn "0.2" 是数据集对 0.20 的省写；直接通配 0.2.* 会命中 2009 年古董，
    # 且 PyPI sdist 0.21 在现代环境构建必挂（曾产出 FIXED 空壳 venv）——sklearn 只装
    # 时代对齐依赖集（跑测试走 workdir 源码 + build_ext 编译），主包走 editable 元数据
    if project == "scikit-learn":
        version = {"0.2": "0.20"}.get(version, version)
    # py 矩阵：xarray 2022.06+ / sympy 1.13+ / astropy 5.3+ / sphinx 7.2+ 要 3.9+
    # （py3.8 下 functools.cache 缺失或 dict[str,Any] 下标=金标假 test_error）
    py = "3.10" if (
        (project == "xarray" and len(version) == 7 and version >= "2022.06")
        or (project == "sympy" and version >= "1.13")
        or (project == "astropy" and version.lstrip("v") >= "5.3")
        or (project == "sphinx" and version >= "7.2")
    ) else "3.8"
    venv = f"{PERF_VENV_DIR}/{project}_{version}"
    probe = scoring._wsl_run(f"[ -f {venv}/.tikutaku_fixed ] && echo OK", 30)
    if "OK" in (probe.stdout or ""):
        return venv
    scoring._wsl_run(f'export PATH="/root/.local/bin:$PATH" && uv venv {venv} --python {py} --seed '
                     f'&& rm -f {venv}/.tikutaku_fixed', 240)
    if not install_cmd:
        # 优先用官方锁定 spec（workdir 里当年 repo 自带的 environment.yml / dev-requirements）
        install_cmd = _spec_install_cmd(spec_workdir, f"{spec_repo or project}", py)
    if not install_cmd:
        tpl = PERF_PKGS.get(project)
        if tpl and project != "scikit-learn":
            # PyPI 短版本号（0.21）要通配（0.21.*）；年份版（2024.05）精确存在；
            # astropy 的 v5.3 带字母前缀要剥掉
            v2 = version.lstrip("vV")
            vspec = v2 if len(v2) >= 6 else f"{v2}.*"
            install_cmd = f"pip install '{tpl.format(v=vspec)}' -i {WS} -q"
    if install_cmd:
        import uuid
        script = f"#!/bin/bash\nexport PATH=\"/root/.local/bin:{venv}/bin:$PATH\"\n{install_cmd}\n"
        sf = PERF_TASKS_ROOT / f"_venv_{uuid.uuid4().hex[:8]}.sh"
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text(script, encoding="utf-8", newline="\n")
        scoring._wsl_run(f"bash {scoring._wsl_path(sf)}", 2400)
    # 恢复段（同 BIP 教训）；uv 在 /root/.local/bin，venv 无 seed pip。
    # 主包 PyPI 兜底：spec 文件常漏传递依赖（sympy 1.14 的 mpmath 等）；
    # sklearn 除外——其老 sdist 在现代环境必挂，改用时代对齐依赖集
    tpl = PERF_PKGS.get(project)
    if project == "scikit-learn":
        main_pkg = ""
        core_pkgs = ("numpy==1.23.5 scipy==1.8.1 pandas==1.3.5 Cython==0.29.37 "
                     "joblib==0.14.1 threadpoolctl==1.1.0 ")
    elif project == "matplotlib":
        # 主包 PyPI 版会与 workdir 源码错配（mpl_toolkits ns 包混解析），改 -e 装
        main_pkg = ""
        core_pkgs = ""
    elif tpl:
        v2 = version.lstrip("vV")
        vspec = v2 if len(v2) >= 6 else f"{v2}.*"
        main_pkg = f"'{tpl.format(v=vspec)}' "
        core_pkgs = ""
    else:
        main_pkg = ""
        core_pkgs = ""
    # 科学栈碎依赖统一补装（astropy 链实证：hypothesis/pyyaml/extension_helpers 等）。
    # 分段安装 + || true：个别包在特定 py 版本装不上不应拖垮整段（& 链一损俱弃的教训）
    base = f'export PATH="/root/.local/bin:{venv}/bin:$PATH" && '
    core = (f'uv pip install --python {venv}/bin/python {main_pkg}{core_pkgs}'
            f'pytest nose '
            f'"typing-extensions>=4.6" wheel -i {WS} -q')
    extra_pkgs = ('hypothesis pyyaml pytest-astropy extension_helpers cython setuptools_scm '
                  f'"setuptools<66"')
    if project == "scikit-learn":
        # extras 的 pytest-astropy/cython 会把 core 钉好的 numpy/Cython 覆盖成不兼容
        # 新版（numpy 1.24.4 + Cython 3.2.9 覆盖 1.23.5 + 0.29.37 实锤），只留构建工具
        extra_pkgs = 'setuptools_scm "setuptools<66"'
    elif project == "matplotlib":
        # setuptools_scm 新版的 scheme 改名警告被 mpl filterwarnings=error 升级，必须
        # 留给专属段钉 6.4.2；cython 同理不装（spec 段已有锁定版本）
        extra_pkgs = 'hypothesis pyyaml pytest-astropy extension_helpers "setuptools<66"'
    steps = [
        core + f' && touch {venv}/.tikutaku_core',
        f'uv pip install --python {venv}/bin/python {extra_pkgs} -i {WS} -q || true',
    ]
    # 源码跑测试时 venv 必须有 xarray dist——entry point 发现（list_chunkmanagers，
    # 2024.05 十题 chunk 测试全 wrong 实锤）与 importlib.metadata 版本查询（0.19 repr
    # 测试 PackageNotFoundError）都查已装 dist，与 sys.path 无关；editable 路径在
    # sys.path 尾部，pytest 的 repo 根 prepend 优先，import 始终命中当前 workdir 源码
    # （已验证）。dask 装最新（2026.x）与老 xarray 不兼容，须钉 calver 对齐时代
    # （2024.05→2024.5）。sklearn 源码 0 处查 dist 元数据，不需要 editable；但其
    # numpy.distutils 老 setup 不支持 PEP 660，装了会挂
    if project == "xarray":
        v2 = version.lstrip("vV")
        if len(v2) == 7 and v2 >= "2023.01":
            calver = f"{v2[:4]}.{int(v2[5:])}"
            steps.append(f'uv pip install --python {venv}/bin/python "dask=={calver}" '
                         f'-i {WS} -q || touch {venv}/.tikutaku_xfail')
        if len(v2) == 7 and "2022.06" <= v2 < "2024.06":
            # numpy 2.x 移除 cumproduct 等别名（8294 实证）；xarray 2024.06 起才适配
            # numpy2。且 numpy 1.x 必须配同代 pandas/scipy（numpy2-ABI 的 pandas 2.2+
            # 在 numpy 1.25 下 import 崩 → 金标假 wrong，2023.07 四题实锤）
            era = ("numpy==1.25.2 pandas==2.0.3 scipy==1.11.4" if v2 >= "2023.01"
                   else "numpy==1.23.5 pandas==1.5.3 scipy==1.9.3")
            steps.append(f'uv pip install --python {venv}/bin/python {era} '
                         f'-i {WS} -q || touch {venv}/.tikutaku_xfail')
        # editable 不在 venv 建时装：entry point 声明（xarray.chunkmanagers 的模块路径）
        # 随 commit 代际迁移（2023=core.daskmanager / 2024=namedarray.dask_compat），
        # 绑死一题的 dist 会错代（8627 实锤）→ score_perf 每题判分前重装（_ensure_dist）
    if project == "matplotlib" and spec_workdir is not None:
        # 主包不能留 PyPI 版：ns 包 mpl_toolkits 混解析 + 老版 API 错配（warning 被
        # mpl 的 filterwarnings=error 升级）——卸掉改 -e workdir；setuptools_scm 钉
        # 6.x（新版的 scheme 改名警告同样被升 error）；spec 常缺测试/绘图依赖，补齐
        steps.append(f'uv pip uninstall --python {venv}/bin/python matplotlib 2>/dev/null; '
                     f'uv pip install --python {venv}/bin/python "setuptools_scm==6.4.2" '
                     f'pillow pyparsing python-dateutil kiwisolver cycler fonttools packaging '
                     f'-i {WS} -q')
        steps.append(f'uv pip install --python {venv}/bin/python --no-deps '
                     f'-e {scoring._wsl_path(spec_workdir)} -q || touch {venv}/.tikutaku_xfail')
    if project == "astropy" and version.lstrip("v") >= "5.0":
        # astropy 5.0+ 依赖 pyerfa（老 erfa 包无 ErfaWarning）；5.2+ 还要拆分出去的
        # IERS 数据包（iers.py 顶层 import，缺则金标假 wrong/test_error）
        steps.append(f'uv pip install --python {venv}/bin/python pyerfa astropy-iers-data '
                     f'-i {WS} -q || touch {venv}/.tikutaku_xfail')
    if project == "scikit-learn":
        shim = PERF_TASKS_ROOT / "_sk_codetype_shim.py"
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text(SK_CODETYPE_SHIM, encoding="utf-8", newline="\n")
        sp = f"{venv}/lib/python3.8/site-packages"
        steps.append(f'mkdir -p {sp} && cp {scoring._wsl_path(shim)} {sp}/sitecustomize.py')
    steps.append(f'[ -f {venv}/.tikutaku_core ] && [ ! -f {venv}/.tikutaku_xfail ] '
                 f'&& touch {venv}/.tikutaku_fixed && echo FIXED')
    import uuid
    script = "#!/bin/bash\n" + base + "\n" + "\n".join(steps) + "\n"
    sf = PERF_TASKS_ROOT / f"_fix_{uuid.uuid4().hex[:8]}.sh"
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(script, encoding="utf-8", newline="\n")
    scoring._wsl_run(f"bash {scoring._wsl_path(sf)}", 1200)
    ok = scoring._wsl_run(f"[ -x {venv}/bin/python ] && echo OK", 30)
    return venv if "OK" in (ok.stdout or "") else None


# 官方 constants.py 的 spec 路径映射（repo 当年自带的锁定依赖）
_SPEC_ENV_YML = {
    "matplotlib/matplotlib": ["environment.yml"],
    "pydata/xarray": ["ci/requirements/environment.yml", "environment.yml"],
    "pandas-dev/pandas": ["environment.yml"],
}
_SPEC_REQS = {
    "pylint-dev/pylint": ["requirements_test.txt"],
    "sympy/sympy": ["requirements-dev.txt"],
}
# spec 文件常漏主包的传递依赖（如 sympy 1.14 的 mpmath）——恢复段统一 PyPI 主包兜底


def _spec_install_cmd(workdir: Path | None, repo: str, py: str = "3.8") -> str:
    """从 workdir 读官方锁定依赖 → pip 安装命令；没有 spec 返回空。"""
    if workdir is None or not Path(workdir).exists():
        return ""
    wd = Path(workdir)
    for rel in _SPEC_REQS.get(repo, []):
        if (wd / rel).exists():
            return f"pip install -r {rel} -i {WS} -q --no-warn-script-location"
    import uuid
    for rel in _SPEC_ENV_YML.get(repo, []):
        f = wd / rel
        if not f.exists():
            continue
        try:
            import yaml
            yml = yaml.safe_load(f.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        lines = []
        # conda yml 是全科学栈（含 cartopy/cdms2 等 conda 专属 C 库），pip 只装核心纯 Python 栈。
        # 裸名（无版本钉）会装最新版打崩老代码 → 钉"时代默认版"（py3.8 ≈ 2020-2021 生态）
        # 时代钉版仅 py3.8 venv（2020-2021 生态）；py3.10 裸名交给 pip 与 spec 内版本约束自解
        era_defaults = ({"pandas": "pandas<1.4", "numpy": "numpy<1.24", "scipy": "scipy<1.8",
                         "matplotlib": "matplotlib<3.6", "dask": "dask<2021.5", "numba": "numba<0.56"}
                        if py == "3.8" else {})
        core = {"numpy", "pandas", "scipy", "matplotlib", "cftime", "dask", "h5py",
                "h5netcdf", "netcdf4", "bottleneck", "fsspec", "zarr", "numba", "pint",
                "sparse", "pytest", "hypothesis", "toolz", "cloudpickle", "packaging"}
        for dep in yml.get("dependencies", []) or []:
            if isinstance(dep, str):
                dep = dep.split("#")[0].strip()  # conda yml 允许行内 # 注释，pip 不认
                if not dep:
                    continue
                # conda 包名变体 → pip 名
                base = dep.split("=")[0].split("!")[0].split("<")[0].split(">")[0].strip()
                base = {"matplotlib-base": "matplotlib", "dask-core": "dask",
                        "dask-expr": "dask", "numpy-base": "numpy"}.get(base, base)
                dep = dep.replace(
                    dep.split("=")[0].split("!")[0].split("<")[0].split(">")[0].strip(), base, 1)
                if base not in core:
                    continue
                if any(c in dep for c in ("=", "<", ">", "!")):
                    lines.append(dep.replace("=", "==", 1) if ("=" in dep and "==" not in dep
                                                               and "!=" not in dep
                                                               and "<=" not in dep and ">=" not in dep)
                                 else dep)
                else:
                    lines.append(era_defaults.get(base, dep))
            elif isinstance(dep, dict) and "pip" in dep:
                lines += [p for p in dep["pip"] if p.split("=")[0].split("[")[0].strip() in core]
        lines = list(dict.fromkeys(lines))  # 去重保序（conda yml 同名多条款）
        if not lines:
            continue
        rf = PERF_TASKS_ROOT / f"_specreq_{uuid.uuid4().hex[:8]}.txt"
        rf.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        wsl_rf = scoring._wsl_path(rf).replace("/mnt", "/mnt")  # workdir 内相对执行更稳
        return (f"pip install -r {wsl_rf} -i {WS} -q --no-warn-script-location")
    return ""


def perf_checkout(bank_id: str, repo_url: str, base_commit: str, timeout: float = 300) -> Path | None:
    """codeload tarball 物化 base 版（幂等）。"""
    workdir = PERF_TASKS_ROOT / bank_id
    if workdir.exists() and any(workdir.iterdir()):
        return workdir
    workdir.mkdir(parents=True, exist_ok=True)
    ru = repo_url.rstrip("/")
    repo = ru.split("github.com/")[1] if "github.com/" in ru else ru
    url = f"https://codeload.github.com/{repo}/tar.gz/{base_commit}"
    tar = workdir.parent / f"{bank_id}.tar.gz"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "tikutaku"})
        with urllib.request.urlopen(req, timeout=timeout) as r, open(tar, "wb") as f:
            f.write(r.read())
        with tarfile.open(tar) as tf:
            root = tf.getmembers()[0].name.split("/")[0]
            tf.extractall(workdir.parent, filter="data")
        import shutil
        if workdir.exists():
            shutil.rmtree(workdir)
        (workdir.parent / root).rename(workdir)
    except Exception:
        return None
    finally:
        tar.unlink(missing_ok=True)
    if workdir.exists():
        # 建本地基线 commit：后续 git checkout . 才能真正重置（全 untracked 时无效）
        subprocess.run(["git", "init", "-q"], cwd=str(workdir), capture_output=True, env=_genv())
        subprocess.run(["git", "add", "-A"], cwd=str(workdir), capture_output=True, env=_genv())
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"],
                       cwd=str(workdir), capture_output=True, env=_genv())
        return workdir
    return None



def _genv() -> dict:
    """git 子进程统一禁 autocrlf（apply/checkout 的 CRLF 转换都会破坏 patch 匹配）。"""
    return dict(**__import__("os").environ,
                GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.autocrlf", GIT_CONFIG_VALUE_0="false")


_META_REJ = (".mailmap", "AUTHORS", "CHANGELOG", "README", "RELEASE", "doc/", "docs/", ".github/")


def _apply_patch(workdir: Path, patch_text: str, tolerate_meta: bool = False) -> bool:
    """应用 patch。tolerate_meta=True 时（金标段用）对无法应用的 hunk 走 --reject，
    只要 reject 的全是 meta 文件（.mailmap/作者名单等——sympy PR 常带，且数据集的
    patch 与 base 偶有已并入偏差）即视为成功；功能源码 hunk 失败仍报 False。"""
    if not patch_text:
        return False
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False, newline="") as f:
        f.write(patch_text)
        tp = f.name
    if not (workdir / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=str(workdir), capture_output=True, env=_genv())
    ap = subprocess.run(["git", "apply", "--whitespace=nowarn", tp], cwd=str(workdir),
                        capture_output=True, text=True, env=_genv())
    ok = ap.returncode == 0
    if not ok and tolerate_meta:
        subprocess.run(["git", "apply", "--whitespace=nowarn", "--reject", tp], cwd=str(workdir),
                       capture_output=True, text=True, env=_genv())
        rejs = [p for p in workdir.rglob("*.rej")]
        bad = [p for p in rejs
               if not any(m in p.as_posix() for m in _META_REJ)]
        for p in rejs:
            p.unlink(missing_ok=True)
        for p in workdir.rglob("*.orig"):
            p.unlink(missing_ok=True)
        return not bad
    Path(tp).unlink()
    return ok


def _reset_base(workdir: Path) -> bool:
    """回 base 态并验证干净：checkout 撤 tracked 修改、clean 清 untracked
    （保留 build/、*.so、*.c 增量编译产物）。git checkout 在 Windows 上会因
    文件锁（WSL 进程刚释放 + Defender 扫描）静默失败（SPF-000008 实锤：
    金标三文件残留模型内容 → apply 上下文不匹配），故用 status 验证 + 重试。"""
    for _ in range(2):
        c1 = subprocess.run(["git", "checkout", "."], cwd=str(workdir),
                            capture_output=True, env=_genv())
        subprocess.run(["git", "clean", "-fdq", "-e", "build/", "-e", "*.so", "-e", "*.c"],
                       cwd=str(workdir), capture_output=True, env=_genv())
        st = subprocess.run(["git", "status", "--porcelain"], cwd=str(workdir),
                            capture_output=True, text=True, env=_genv())
        dirty = [l for l in (st.stdout or "").splitlines()
                 if l and not l[3:].endswith(("build/", ".so", ".c"))]
        if c1.returncode == 0 and not dirty:
            return True
        time.sleep(2)
    return False


def _run_timed(workdir: Path, venv: str, node: str, timeout: float = 900,
               pytest_args: str = "") -> tuple[str, float | None]:
    """跑一次效率测试 → (状态, pytest 报告秒数)。状态: ok/fail/collect_error"""
    script = (
        "#!/bin/bash\n"
        f'cd {scoring._wsl_path(workdir)}\n'
        "export SKLEARN_SITE_JOBLIB=1\n"
        f'{venv}/bin/python -m pytest -q {pytest_args}"{node}" > /tmp/perf_out_$$.txt 2>&1\n'
        "rc=$?\n"
        "tail -2 /tmp/perf_out_$$.txt\n"
        "rm -f /tmp/perf_out_$$.txt\n"
        "exit $rc\n"
    )
    import uuid
    sf = PERF_TASKS_ROOT / f"_perf_{uuid.uuid4().hex[:8]}.sh"
    sf.write_text(script, encoding="utf-8", newline="\n")
    cp = scoring._wsl_run(f"bash {scoring._wsl_path(sf)}", timeout)
    body = (cp.stdout or "") + (cp.stderr or "")
    # pytest 汇总行形如 "1 passed, 4 warnings in 1.02s"（可能带颜色码）
    m = re.search(r"in\s+(\d+(?:\.\d+)?)s", body)
    dur = float(m.group(1)) if m else None
    if cp.returncode == 0:
        return "ok", dur
    if cp.returncode in (2, 3, 4):
        return "collect_error", dur
    return "fail", dur


_C_REPOS = {"astropy", "scikit-learn", "matplotlib"}  # 源码跑测试需 build_ext 的 repo
# （matplotlib 的 _c_internal_utils 不编译则 conftest import 崩）


def _ensure_build(workdir: Path, venv: str, timeout: float = 2400, serial: bool = False) -> bool:
    """C 扩展 repo 首次判分前 build_ext（gcc-10；build/ 产物 untracked 不被 git 清除，增量）。

    matplotlib 必须串行：多 ext 共享源文件（py_converters.cpp 等）并行编译写同一
    .o 互踩 → file format not recognized。
    """
    import uuid
    jflag = "" if serial else " -j8"
    script = ("#!/bin/bash\n"
              f"cd {scoring._wsl_path(workdir)}\n"
              f'export PATH="{venv}/bin:$PATH"\n'
              "export SKLEARN_SITE_JOBLIB=1\n"  # sklearn 0.21 bundled joblib 的 cloudpickle 在 py3.8 崩
              "command -v gcc-10 >/dev/null && export CC=gcc-10\n"
              "command -v g++-10 >/dev/null && export CXX=g++-10\n"
              f"python setup.py build_ext --inplace{jflag} > /tmp/perf_build_$$.log 2>&1\n"
              "rc=$?\n"
              "rm -f /tmp/perf_build_$$.log\n"
              "exit $rc\n")
    sf = PERF_TASKS_ROOT / f"_build_{uuid.uuid4().hex[:8]}.sh"
    sf.write_text(script, encoding="utf-8", newline="\n")
    cp = scoring._wsl_run(f"bash {scoring._wsl_path(sf)}", timeout)
    return cp.returncode == 0


def _ensure_dist(workdir: Path, venv: str, project: str) -> bool:
    """xarray 每题判分前重装 editable dist：entry point 发现与 importlib.metadata
    版本查询都查 venv 里已装 dist（与 sys.path 无关），但 entry point 声明的模块路径
    随 commit 代际迁移（2023=core.daskmanager / 2024=namedarray.dask_compat）——
    绑死一题的 dist 在别的 commit 上会 import 不到模块（8627 实锤）。"""
    if project != "xarray":
        return True
    cp = scoring._wsl_run(
        f'export PATH="/root/.local/bin:$PATH" && uv pip install --python {venv}/bin/python '
        f'--no-deps -e {scoring._wsl_path(workdir)} -q', 120)
    return cp.returncode == 0


def score_perf(workdir: Path, patch_files: dict[str, str], venv: str, node: str,
               gold_blob: dict, runs: int = 5, timeout: float = 900,
               project: str = "") -> dict:
    """效率判分：写回模型 patch → 计时 N 次（预热 1）→ 与金标基线比。

    gold_blob: unseal 出的 JSON（duration_changes/human_performance 等）——当前实现
    用"本地重测金标"作对比基准（同机同窗口更公平），官方数据做 sanity 参考。
    """
    t0 = time.time()

    def out(v: str, **kw):
        d = {"verdict": v, "sec": round(time.time() - t0, 1)}
        d.update(kw)
        return d

    # astropy 5.3.dev 的 logger fixture 会替换 warnings.showwarning，而 pytest 的
    # warnings 插件在 catch 退出时恢复原函数 → teardown 报 "not set by this logger"
    # （v5.3 四题实锤）——对 astropy 禁用 warnings 插件
    pa = "-p no:warnings " if project == "astropy" else ""

    for p in patch_files:
        if scoring._is_test_path(p):
            return out("apply_fail", reason=f"test_file_rejected:{p}")
    if not patch_files:
        return out("apply_fail", reason="no_source_changes")
    if project in _C_REPOS and not _ensure_build(workdir, venv, serial=(project == "matplotlib")):
        return out("error:build_fail")
    if not _ensure_dist(workdir, venv, project):
        return out("error:dist_fail")
    # 1) 模型 patch 落地（含 .pyx/setup.py 等构建文件时，落地后必须增量重编 .so，
    #    否则新代码 import 旧 .so 符号 → collect_error（13243 的 seq_dataset 改名实锤））
    saved = {}
    for p, content in patch_files.items():
        fp = workdir / p
        if not fp.exists():
            return out("apply_fail", reason=f"no_such_file:{p}")
        saved[p] = fp.read_text(encoding="latin-1")
        fp.write_text(content, encoding="latin-1", newline="\n")
    if project in _C_REPOS and not _ensure_build(workdir, venv, serial=(project == "matplotlib")):
        return out("error:build_fail")
    try:
        # 2) 预热 + 计时（效率测试有 flaky：网络/温度/GC 抖动导致偶发 fail，首败重试一次）
        _run_timed(workdir, venv, node, timeout, pytest_args=pa)
        durs = []
        st = "ok"
        for _ in range(runs):
            st, dur = _run_timed(workdir, venv, node, timeout, pytest_args=pa)
            if st != "ok" or dur is None:
                st, dur = _run_timed(workdir, venv, node, timeout, pytest_args=pa)  # flaky 重试
            if st != "ok" or dur is None:
                # collect_error = 模型补丁连 pytest 收集都过不了（import/syntax 被改坏）——
                # 与 D4J compile_error 同语义的有效作答失败，不再无限重测
                # （seaborn/xarray/pylint 7 题实锤：金标审计时环境全通过）
                v = {"fail": "wrong_answer", "collect_error": "compile_error"}.get(st, "error:test_error")
                return out(v, detail=f"{st}:{dur}")
            durs.append(dur)
        durs.sort()
        model_med = durs[len(durs) // 2]
        # 3) 回 base → 应用金标（先打：金标与 test_patch 可含共同新增文件如 docs changelog）。
        #    checkout 只撤 tracked 修改，untracked 新增（test_patch 的文件）必须 clean 掉，
        #    否则金标 apply 撞 already exists（astropy 5 题实锤）
        if not _reset_base(workdir) or not _apply_patch(workdir, gold_blob["patch"], tolerate_meta=True):
            # 瞬态（Windows 文件锁竞态）兜底：稍候重试一轮
            time.sleep(2)
            if not _reset_base(workdir) or not _apply_patch(workdir, gold_blob["patch"], tolerate_meta=True):
                return out("error:gold_apply_fail")
        _apply_patch(workdir, gold_blob.get("test_patch", ""))
        if project in _C_REPOS and not _ensure_build(workdir, venv,
                                                    serial=(project == "matplotlib")):
            return out("error:gold_build_fail")
        _run_timed(workdir, venv, node, timeout, pytest_args=pa)
        gdurs = []
        for _ in range(runs):
            st, dur = _run_timed(workdir, venv, node, timeout, pytest_args=pa)
            if st == "ok" and dur:
                gdurs.append(dur)
        if len(gdurs) < 3:
            return out("error:gold_timing_fail")
        gdurs.sort()
        gold_med = gdurs[len(gdurs) // 2]
        speedup = gold_med / model_med if model_med else 0
        # 判优：模型中位不劣于金标中位的 1.25 倍 → pass（留 25% 计时噪声裕度）
        verdict = "pass" if model_med <= gold_med * 1.25 else "insufficient_speedup"
        return out(verdict, model_med=model_med, gold_med=gold_med,
                   speedup=round(speedup, 2), runs=runs)
    finally:
        # 回到 base commit 态（git checkout 现在有效：物化时已建基线）。
        # test_patch 由调用方在每次判分前重打。build/与 .so 保留（下次增量重编）。
        _reset_base(workdir)
