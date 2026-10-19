# 操作手册（RUNBOOK）

## 环境
- Python 入口（2026-10-11 起）：
  - **判分/D4J/webapp 一律用** `D:/tikutaku/venv/Scripts/python.exe`（D 盘 venv，绕开项目树 Low 完整性标记；见下方四连坑）
  - 纯数据脚本（不碰 msys/perl/java）用 `uv run python ...` 也行；新依赖装法：
    `UV_PROJECT_ENVIRONMENT=D:/tikutaku/venv uv sync`
- **首选网络**：本机 Clash Verge 代理 `127.0.0.1:7897`，开启后所有下载加前缀
  `export HTTPS_PROXY=http://127.0.0.1:7897 HTTP_PROXY=http://127.0.0.1:7897`
  并可直连 HF 官方站（`HF_ENDPOINT=https://huggingface.co`）
- **无梯子后备**：HF 镜像站 + 禁 Xet 已写入 `tools/env.py`（`HF_ENDPOINT=https://hf-mirror.com`、`HF_HUB_DISABLE_XET=1`）
- 金标密钥：`bank/seals/.key`（丢了密钥 = 所有金标作废，务必备份）
- ASCII junction：`D:\tikutaku\tb → E:\AI\测试题库`（D4J 依赖 URL 走它；重建：
  `cmd /c "mklink /J D:\tikutaku\tb E:\AI\测试题库"`）

## 日常流程

### 1. 更新数据层
```bash
uv run python tools/download_datasets.py     # 幂等，已完成的源跳过
bash tools/fetch_tarballs.sh                 # 仓库 tar 补拉（github 断连时用）
```

### 2. 重建索引 + 防污染标记 + 视图
```bash
uv run python tools/build_index.py           # 全量重建（含金标重封存）
uv run python tools/cutoff_filter.py         # 按模型 cutoff 打标（模型表在 tools/env.py）
uv run python tools/export_views.py          # 生成 bank/views/*.json
```

### 3. 按需拉题（一次 10~50 道）
```bash
# 新鲜锚题 30 道（晚于全部模型 cutoff，Docker 就绪后拉镜像）
uv run python tools/pull_tasks.py --view anchor --n 30 --anchor --mirror docker.1ms.run
# 只看元数据不碰环境
uv run python tools/pull_tasks.py --view security --n 10 --no-env
# 防污染视图：只测某模型"没背过"的题
uv run python tools/pull_tasks.py --view fresh_all --n 20 --model glm-5.3
```

### 4. 工作区物化（CLI 评测用）+ 金标预检
```bash
uv run python tools/prepare_workspace.py --report logs/last_pull_report.json
uv run python tools/audit_task.py --report logs/last_pull_report.json --dataset <同源HF名>
```

### 5. 评测出榜
**图形界面（推荐）**：双击根目录 `启动测试台.bat`（或 `uv run python webapp/server.py`），
浏览器打开 http://127.0.0.1:8790。
- **连接（三项必填，不允许空）**：模型名 + API Key + 接口地址。协议（OpenAI/Anthropic 兼容）
  由接口地址自动探测——地址含 "anthropic" 先试 Anthropic，否则先试 OpenAI，哪个调用成功用哪个；
  「测试连接」和「开始评测」前都会探测，两个协议都不通直接报错，不进跑批。
  （GLM 订阅用户：接口地址填 https://open.bigmodel.cn/api/anthropic，Key 填订阅 token）
- **默认模式**：总题量 + 难度配比（N1 极难~N5 基础，预设四种 + 自定义）+ 随机种子
  （固定数字 = 组卷可复现）→ 后端按库存随机组卷 → 实时进度 + 五档评分报告 + 模型横向对比。
- **专家模式**：每题 token 上限、单题作答时限、温度、判分超时、追加系统提示词、并发。
- 题池只含**有答题渠道且可自动判分**的题（算法卷 1046 题）；
  修码/性能等题待判分器接入后在 `harness/runner_core.py: ELIGIBLE_VERIFIERS` 登记入池。
- N 档难度映射：N1←L5 … N5←L1（level 由 GLM-5.3 实测通过率标定，对新模型是有效难度序）。

```bash
# CLI 等价路径 —— API 适配器，GLM（主力，走订阅套餐的 Anthropic 协议端点，密钥自动从 ~/.claude/settings.json 读）
# 并发策略（2026-10-11 用户实测更新）：默认 4，夜间低谷可试 6；撞限流（429/1302）一个一个往下减
uv run python harness/run.py --view lcb_easy --n 100 --adapter api --model glm-5.3 --concurrency 4 --skip-done
# 专家参数（CLI 同样可用）：--max-tokens --temperature --task-timeout --score-timeout --system-extra
uv run python harness/run.py --view lcb_hard --n 50 --adapter api --model some-model \
    --protocol openai --base-url https://api.example.com/v1 --api-key sk-xxx --max-tokens 16384
# 其他 OpenAI 兼容模型（如 DeepSeek，需自备余额）
uv run python harness/run.py --view lcb_easy --n 100 --adapter api \
    --model deepseek-chat --base-url https://api.deepseek.com

# CLI 适配器（claude/codex/zcode 自动探测）
uv run python harness/run.py --report logs/last_pull_report.json --adapter cli --tool claude
```
成绩在 `results/run-*/`（results.jsonl + leaderboard.md）。标定回填：`uv run python tools/backfill_calibration.py --model <模型>`。
注意：GLM 的 OpenAI 端点（paas/v4）与订阅套餐不通用（余额报 1113 错），必须走 Anthropic 协议端点。

### 6. 镜像缓存治理
```bash
uv run python tools/image_cache.py status    # 盘点 + 上限
```
上限 200GB，LRU 自动淘汰，锚题（--anchor 拉取）永不淘汰。

## Defects4J 本地判分（2026-10-11 建成，零 Docker）

- **链路**：`run_one(d4j_tests)` → `scoring.d4j_checkout`（按需物化到 `E:\AI\d4j_work\tasks\<bank_id>`，ASCII 路径）
  → 题面+触发测试+金标涉及源文件全文（oracle 定位，latin-1 无损往返）→ `api_agent.solve_patch`
  （SEARCH/REPLACE 块协议，比手写 diff 抗格式噪声）→ `scoring.score_d4j`（git 重置→写回→compile→F2P 类级合并跑）。
- **verdict**：pass / wrong_answer / compile_error / apply_fail / error:empty_reply / error:checkout_fail。
  金标冒烟：Cli-1 / Math-5 / Jsoup-1 全 PASS。802 题入池（verifier=d4j_tests），52 题 retired（50 空壳金标+2 checkout_fail）。
- **跑法**：`D:/tikutaku/venv/Scripts/python.exe harness/run.py --view d4j_first20 --adapter api --model glm-5.3 ...`

### ⚠️ 2026-10-11 四连坑（新环境必读）
1. **项目目录树是 Low 完整性标记**（ZCode 沙箱所加）：树内 exe（venv python、vendor JDK）启动的进程都是
   Low token → **MSYS 程序（perl/bash）直接崩**（NtCreateDirectoryObject 0xC0000022）、java 写 `~/.maven` 被拒。
   **解**：生产 venv 建在 `D:\tikutaku\venv`（树外）、JDK 用 D 盘副本 `D:\tikutaku\jdk-11.0.32.1+1`。
   以后**所有跑判分的命令用 `D:/tikutaku/venv/Scripts/python.exe`**，不要在项目树内建 venv。
2. **cygwin perl 路径风格**：PERL5LIB/JAVA_HOME/`-w` 参数必须 POSIX 风格（`/e/...`），Windows 风格被 `:` 拆断。
3. **ant 不在系统 PATH**：D4J 自带 `major/bin/ant`（shell 包装 java -jar ant-launcher），_d4j_env 已注入。
4. **build.xml 的 `file:///d/...` URL 缺盘符冒号**：Windows java 解析失败且 `ignoreerrors=true` 静默跳过 →
   编译缺依赖假失败。`_fix_d4j_build_xml` 在 checkout 后和每次判分重置后改写 `file:///d/ → file:///D:/`（幂等）。
   另外 D4J 框架路径必须走 ASCII junction `D:\tikutaku\tb → E:\AI\测试题库`（URL 含中文 java 也挂）。
- 判分输出解析看 **stderr**（cygwin perl 的 stdout/stderr 分流与预期不同），成功以 rc==0 为准。

## BugsInPy 本地判分（2026-10-11 晚建成，WSL 路线）

- **链路**：`run_one(bip_tests)` → `bip_checkout`（codeload tarball 物化到 `D:\tikutaku\bip_tasks`）
  → `bip_prepare_test`（**fixed 版测试文件覆盖**——F2P 测试方法只在 fixed 版存在，BIP 官方语义）
  → `bip_venv`（WSL `/root/bip_venvs/<项目>` py3.8 + requirements，首次建立 10-20 分钟）
  → `solve_patch`（SEARCH/REPLACE）→ `score_bip`（判分脚本落 `_judge.sh` 文件执行，rc 透传）。
- 金标冒烟（2026-10-11）：black 3/3、luigi 3/3、tqdm 2/3（tqdm-2 为 py3.6→3.8 行为偏差，非判分器问题）。
- **跑法**：同 D4J 命令换 `--view bip_first20`。venv 建好后每题判分秒级。
- 492/501 题 fail_to_pass 已回填（enrich_bip_metadata.py）；501 全解析。

### ⚠️ BIP 六坑（2026-10-11 实测，全固化在 scoring.py）
1. requirements 是当年 **Windows pip freeze**：混 pywin32 等平台专属包（Linux 全盘失败）→ grep 过滤
2. requirements 多 **UTF-16** 编码：grep 匹配失效 → iconv 转 UTF-8
3. 源里没有的死包（requests-async==0.5.0）→ 整体失败后**逐包容错**（SKIP 记 /root/bip_skipped.txt）
4. 老 sdist（tornado 5.0）在新 setuptools 下 metadata 构建崩 → **先降 setuptools<71** 再装
5. requirements 把 typing-extensions 降级打坏 exceptiongroup → 装完恢复 4.x；setuptools≥75 不支持 py3.8
6. `-e git+` editable 的 clone 必须 cd /root 落 WSL ext4（/mnt 上 clone 慢 + 触发 WSL 性能告警弹窗）
- 另外：**wsl.exe 参数层会吞 `$?`/`${...}`**（false;echo $? 得 0）——所有 WSL 判分命令一律落脚本文件执行；
  PATH 注入要带引号（继承的 Windows PATH 含空格路径）。

## SWE-Perf 效率判分（2026-10-12 建成，WSL 路线）

- **链路**：`run_one(efficiency_test)` → `perf_scoring.perf_checkout`（codeload tarball + 本地 git 基线 commit）
  → test_patch → oracle 函数提示 + 金标涉及源文件 → `solve_patch` → `score_perf`：
  模型 patch 计时 N 次（预热 1 + 中位）→ 回 base 重打金标同窗口计时 → model_med ≤ gold_med×1.25 = pass。
- 金标自判验证：pass，speedup 0.98（sympy-14331）。首测真实判定：SPF-000139 wrong_answer（模型优化无效）。
- **Perf 五坑（全固化在 perf_scoring.py）**：
  1. git autocrlf 破坏 patch 匹配（_genv() 统一注入，apply/checkout 都要）
  2. 全 untracked 仓库 `git checkout .` 无效 → 物化时 `git init+add+commit` 建基线
  3. repo 字段可能是 owner/name 非 URL（perf_checkout 兼容两种）
  4. sympy 1.1 的 matrices.py（8000 行）单轮思考 65536 不够 → empty_reply（大文件 Perf 题留 CLI 模式）
  5. 计时必须用 pytest 内部时长（WSL wall 含 ~3s 启动噪声）
- **Perf 环境矩阵（2026-10-13 大修，13 坑固化在 perf_venv）**：
  1. **源码跑测试时 venv 必须有本项目 dist**——xarray 的 entry point 发现（list_chunkmanagers）
     与 importlib.metadata 查询查的是已装 dist，与 sys.path 无关；`uv pip install --no-deps -e <workdir>`
     注册元数据，import 仍命中 workdir 源码（editable 路径在 sys.path 尾，pytest repo 根 prepend 优先）
  2. dask 装最新（2026.x）与老 xarray 不兼容 → calver 钉 dask==同年月（2024.05→2024.5）
  3. sklearn 主包不走 PyPI（老 sdist 现代环境必挂）→ 时代依赖集：numpy==1.23.5 scipy==1.8.1
     （pinv2 在 1.9 移除）pandas==1.3.5 Cython==0.29.37 joblib==0.14.1 threadpoolctl==1.1.0
  4. sklearn 0.21 bundled joblib 的 cloudpickle 按 py3.7 签名（15 参）调 CodeType → py3.8 崩；
     `SKLEARN_SITE_JOBLIB=1` 走外部 joblib，但 testing.py 硬 import externals → **sitecustomize
     垫片**（import 窗口内替换 CodeType 垫 posonlyargcount=0；直接换 types.CodeType 会破坏
     numpy 的 isinstance 检查）
  5. matplotlib 主包必须卸 PyPI 改 -e（mpl_toolkits 是 ns 包，site-packages 版与源码错配）；
     **串行编译**（多 ext 共享 .pyx 并行写同一 .o 互踩）；setuptools_scm 钉 6.4.2；CXX=g++-10
  6. astropy 5.x 要 pyerfa+astropy-iers-data；5.3+ 要 py3.10（functools.cache）；5.3.dev 的
     logger 双 disable 问题 4 题未解（搁置）
  7. sphinx 4.0 钉 jinja2==3.0.3+markupsafe==2.0.1（environmentfilter 在 3.1 删）；7.2+ 要 py3.10
  8. **numpy 1.x/2.x 是 ABI 分界**：降 numpy 必须连带同代 pandas/scipy（2023.07 实锤：numpy 1.25
     + pandas 2.3（numpy2 ABI）→ import 崩 → 金标假 wrong 五题）；era 组合固化在 perf_venv
  9. sklearn "0.2" 是数据集对 0.20 的省写（通配 0.2.* 命中 2009 古董）
  10. patch 含 .pyx/setup.py 时，文件写回后**必须增量重编 .so**（13243：seq_dataset 改名，
      新 pyx import 旧 so 符号 → collect_error）；score_perf 段1/段3 后各跑一次 _ensure_build
  11. **金标 patch 含文件删除 → 文件快照协议不可表达 → 剔除**（13243 全库唯一，
      marked unsupported:structural_patch）
  12. 金标 hunk 与 base 有偏差的 meta 文件（.mailmap 等）→ --reject + 只容忍 meta 的 rej
  13. 判分日志审计**严禁双进程并发**（perf_gold_audit.json 增量合并非原子）
  14. xarray 的 editable dist **必须每题判分前重装**（_ensure_dist）：entry point 声明的
      模块路径随 commit 代际迁移（2023=core.daskmanager / 2024=namedarray.dask_compat），
      venv 绑死一题的声明在别的 commit 上 import 不到（8627/8277 镜像实锤）
  15. astropy 5.3.dev 的 logger fixture 替换 warnings.showwarning，pytest warnings 插件
      在 catch 退出时恢复原函数 → teardown 假错（v5.3 四题）→ astropy 判分加
      `-p no:warnings`
- **Perf 金标审计终态（2026-10-13）**：96 题 = 95 pass + 1 剔除（13243 结构变更）。
  venv 矩阵：xarray×7 sklearn×2 astropy×5 matplotlib×2 sphinx×4 sympy×3 seaborn×2
  requests×2 pylint×2（/root/perf_venvs，共 6.4GB）。
- **BIP C 扩展项目（pandas）判分三件套**：gcc-10 + g++-10（gcc13 对 Cython 0.29 产物 -Werror=array-bounds 误报，
  且需软链 /usr/bin/cc、/usr/bin/c++ → gcc-10 套件）；setup.sh 剥 --force 走增量编译（全量 15 分钟→增量 5 分钟）；
  判分 timeout ≥2400。pandas-1 金标 PASS 已验证。
- **bip_gold_audit.py 有卡死毛病**（stdout 管道空挂，四次复现未根治）——pandas 类长判分直接用
  score_bip 手动组装调用（见 /d/tikutaku/pandas_manual4.log 的成功模式）。

## Docker 环境（2026-10-10 已建成，WSL2 路线）
- **WSL2**：发行版 `tikutaku`（Ubuntu 24.04，docker 精简 rootfs 自装 systemd），vhdx 在 `E:\AI\vm\wsl`，
  资源 14GB 内存/12 核（`~/.wslconfig`，**networkingMode=mirrored**——WSL 共享宿主 localhost）
- **docker-ce**：systemd 管理自启；registry-mirrors 首选 `docker.1ms.run`（**长尾题库镜像 OK**），
  ⚠️ DaoCloud 公益源有白名单，sweb.eval 系镜像会被拒（"not in the allowlist"）
- **Windows 侧调用**：`bin/docker.cmd`（包装 `wsl -d tikutaku -- docker`），pull_tasks 等脚本用
  `PATH=bin:$PATH` 或全路径调用
- 常用：`wsl -d tikutaku` 进入；`wsl --shutdown` 重启发行版；WSL 内题库路径 `/mnt/e/AI/测试题库`（中文路径在
  Git Bash→wsl.exe 传参时会编码损坏，复杂命令用 heredoc stdin 管道：`wsl.exe -d tikutaku -- bash <<'EOF'`）
- 已验证闭环：锚题镜像 5.42GB 拉取 + 容器 /testbed 工作区在位（2026-10-10，SWLM-000039）

## 排位赛（锚卷常态化评测，2026-10-19 建成）

固定锚卷定期重跑，产出排行榜 + 时间序列 + 漂移报警。三个用途：多模型锚卷成绩重切难度档（多模型尺替换单模型尺）、模型升级回归测试、题目通过率突变 = 环境漂移报警。

### 生成锚卷（冻结语义：同名不可覆盖，轮换 = 新建 v2）
```bash
# 正式卷 anchor-v1：100 题（algo:40 java:20 py:25 perf:15，域内档位贴库存比例，seed 记录在卷内）
uv run python tools/make_anchor.py --name anchor-v1 --seed 20261019
# 查看已有锚卷
uv run python tools/make_anchor.py --list
```
锚卷存 `bank/anchors/<name>.json`：bank_ids + per_task 难度档快照（冻结当刻口径，重切后旧卷仍按冻结口径显示）。

### 开赛（手动触发，节奏建议：每周一测 + 模型升级/换 provider 必测）
- **webapp**：出卷卡顶部选锚卷 → 组卷参数自动置灰 → 开始评测。锚卷模式**强制不跳题**（回归测试语义：同模型重跑同卷不能被 skip-done 跳掉），跑完自动进"⑤ 排位赛榜单"。
- **CLI**：`harness/run.py --anchor anchor-v1 --model <m> --base-url <u> --api-key <k> --concurrency 4`（CLI run 也写 config.json，与 webapp 同构入榜）。
- 多模型对比 = 逐个换模型凭据各跑一轮（凭据不落盘红线：不做自动轮换）。100 题 GLM 5 并发约 3 小时，白天跑（校园网 12 点断网）。
- 榜单卡内容：跨模型排名（每模型最新一轮）+ 各域拆分 + 通过率趋势折线（每模型一线）+ 漂移检测（同模型最新两轮逐题翻转清单；单题翻转属采样波动，集中翻转才查环境）。

### 难度重切（攒够 3+ 模型锚卷成绩后）
```bash
uv run python tools/recalibrate_from_anchor.py --anchor anchor-v1            # dry-run 建议表
uv run python tools/recalibrate_from_anchor.py --anchor anchor-v1 --apply    # 备份后落盘
```
- 逻辑：每题多模型通过率 → 域内分位切档（algo 五档 / 三源三档）→ 只动锚卷内的题。
- 语义提示：三源旧尺是"快pass/慢pass/失败"（单模型 GLM 行为尺），重切后是"多模型通过率尺"，两把尺都有效，报告注明用的是哪把。
- `--apply` 自动备份到 `bank/backups/index-<ts>.parquet` + 清单 `logs/recalibrate_<ts>.json`；回滚 = 备份覆盖 `bank/index.parquet`；**生效需重启 webapp**（池有模块级缓存）。

## 防污染红线
- 任务环境一律经 `strip_vcs.py`（剥 .git；agent 翻 git log 抄 patch 是实证过的作弊路径）
- 金标只存 `seals/`（加密），predictions 临时文件检完即删
- 报告成绩必须注明题目 vintage（created_at 年月）
- 合成题（notes=synthetic）天然零污染，可跨模型复用

## 判分质量红线（2026-10-10 审计教训，避免再犯）
- **推理模型跑批 max_tokens ≥16384**：思考链耗尽 token 会得到"空回复"，被判 compile_error 播种成假难题（本次 231 题、占 22%）。score_oj 已对空代码记 `note=empty_answer`，审计脚本可一键识别。
- **全量标定的固定顺序**：跑完 → `tools/audit_scoring_noise.py` 审计（空回复/格式噪声/运行时崩归因）→ 处理翻案与作废 → `tools/recalibrate_after_audit.py` / `backfill_calibration.py` 回填。**不要**拿着原始 results 直接回填 level。
- **输出比对一律走 `scoring.norm_eq`**（空白折叠/字面量等价/数值容差/逐行等价）：本次 46 题被 `1, 3` vs `1,3` 之类格式差误判。新增任何判分器必须复用或对齐该语义，命中非严格等价记 `score.normalized` 留痕。
- **失败模式要可区分**：运行时异常（如模型没适配 `[1, 2, 3]` 输入格式）与输出不匹配不能都叫 wrong_answer，判分输出里分开标注，否则事后归因只能翻 stderr。
- **评测统一最高思考强度**（2026-10-10 用户定的公平性规则）：现代模型不支持温度等采样参数，改用思考强度。`--thinking-level`/前端"思考强度"默认 max；映射：OpenAI 系 `reasoning_effort=high`，Anthropic 协议 `thinking.budget_tokens`（GLM 端点实测支持；budget=min(档位, max_tokens-1000)），Gemini 3 走 OpenAI 兼容层映射，DeepSeek R 系无参数自动降级。端点不认参数时自动去掉重试（不计重试次数）并在 usage 记 `thinking_degraded: true`。温度参数仅对旧式采样模型有效。

## 已知问题
- GitHub 主站间歇断连 → 用 `tools/fetch_tarballs.sh`（codeload 通道）
- HF 官方站不通 → 已走 hf-mirror；gated 数据集需 `huggingface-cli login`
- Docker Hub 直连不通 → pull_tasks --mirror 或 Docker Desktop registry-mirrors
