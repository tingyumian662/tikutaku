# tikutaku — 私有可复现 LLM 修码评测题库工厂

> **TL;DR** — A factory for building your *own*, private, reproducible LLM coding-benchmark bank:
> ingest 130k+ tasks from 19 public sources into one unified schema, seal gold patches with
> symmetric encryption, score four task domains (algorithm / bug-fix / performance) locally
> with deterministic verifiers, calibrate per-domain difficulty tiers from real model runs,
> then compose exam papers with locked difficulty ratios through a web UI. **No task data is
> distributed in this repo** — only the tooling, methodology, and a step-by-step replication
> guide (see "从零复刻" below) so you can rebuild the whole thing from public upstream sources.

这不是"又一个评测框架"。lm-eval / OpenCompass / SWE-bench 回答的是"怎么在**公共基准**上跑模型"；
tikutaku 回答的是另一个问题：**怎么从零造一座自己的私有题库**——题目可以自由扩充、金标保密、
难度自标定、试卷可复现、防模型背题。适合企业内测、竞赛出题、实验室私有评测等不愿把题面公开的场景。

用本工厂建成的那座题库（数据不随本 repo 分发）的实测规模：

| 试卷域 | 题源 | 可测池 | 判分方式 |
|---|---|---|---|
| 算法 algo | LiveCodeBench | 1,051 | OJ 断言（归一化输出比对） |
| Java 修码 java_fix | Defects4J | 802 | F2P 测试（本地 JDK+ant，零 Docker） |
| Python 修码 py_fix | BugsInPy | 376 | F2P 测试（WSL 隔离 venv） |
| 性能优化 perf | SWE-Perf | 100 | 优化前后基准计时对比 |
| （数据层合计） | 19 源 | 127,488 题已入库索引 | Docker 池休眠中，按需拉取 |

## 核心设计

```
                    ┌────────────────────────────────────────────────┐
  上游公共数据源     │  数据层 sources/  （HF 数据集 + harness 仓库）    │   ← 全量、<10GB
  19 源 13 万题 ──► │  统一 schema（七组字段） · cutoff/去重/防污染      │
                    └───────────────┬────────────────────────────────┘
                                    │ build_index + seal_gold
                    ┌───────────────▼────────────────────────────────┐
                    │  题库层 bank/                                    │
                    │  index.parquet（13 万行） · views/（选题视图）     │
                    │  seals/（金标 Fernet 加密，密钥单文件保管）         │
                    └───────────────┬────────────────────────────────┘
                                    │ 按需物化（剥 .git、工作区）
                    ┌───────────────▼────────────────────────────────┐
                    │  运行层 harness/ + webapp/                       │
                    │  双适配器（OpenAI/Anthropic API · CLI agent）     │
                    │  四域判分器 · 噪声审计 · 难度播种 · 组卷出榜        │
                    └────────────────────────────────────────────────┘
```

四条贯穿全部代码的设计原则：

1. **防污染是硬约束不是选项**。每题带 `created_at`，晚于模型知识截止日才对该模型有效
   （`cutoff_filter.py` 产出 per-model 视图）；任务环境强制剥 git 历史（SWE-rebench 实测
   agent 会翻 `git log` 抄官方 patch，`strip_vcs.py`）；SWE-bench Verified 这类公开答案
   满天飞的源只抽样做 harness 对表、不入正池。
2. **金标与判分分离**。金标 patch 用 Fernet 对称加密存 `bank/seals/`（`seal_gold.py`），
   密钥单文件 `.key` 保管（丢失=金标全废，务必离线备份）；判分器只做确定性验证
   （FAIL→PASS / OJ 断言 / 计时对比），不引入 LLM 裁判——裁判是噪声源。
3. **先审计工具噪声，再谈模型差异**。判分器自身的分类错误（格式噪声、思考链耗尽空回复、
   pytest 退出码误读）会把"没测出能力"记成"模型不行"。本库实测：一轮离线噪声审计
   （`audit_scoring_noise.py`，零 API 成本）修正了 16-23 个百分点的测量偏差。
   任何新判分器接入后，第 0 步永远是对已有 results 做噪声归因。
4. **难度是"题目×模型群体"的联合属性**。不同任务域用**独立难度尺**（算法域五档
   L1-L5 按锚模型实测通过率标定；修码/性能域三档：快 pass=易 / 慢 pass=中 / 失败=难），
   域间难度不可直接比较；同档内用 completion_tokens 作连续难度序（`tier_rank`）。
   单锚模型自证有偏——凑齐 3+ 异源模型前不重切档，表结构已预留 per-model 列。

## 快速开始

环境要求：Windows 10/11 + WSL2（或任意 Linux）；Python ≥3.11；[uv](https://docs.astral.sh/uv/)；
约 10GB 磁盘给数据层。Java/Python 修码判分另需 JDK11 与 WSL 内 py3.8（见 Phase 2）。

```bash
git clone <this-repo> && cd tikutaku
uv sync                                       # 依赖
uv run python tools/download_datasets.py      # 拉数据层（HF 镜像站，国内可直连）
uv run python tools/build_index.py            # 统一索引 + 金标加密封存
uv run python tools/cutoff_filter.py          # 按模型截止日打标
uv run python tools/export_views.py           # 选题视图
# 用任一 OpenAI 兼容模型跑一卷算法题（零镜像、零 Docker）：
uv run python harness/run.py --view lcb_all --adapter api \
    --model glm-5.3 --concurrency 4 --skip-done --thinking-level max
uv run python webapp/server.py                # Web 组卷测试台 → http://127.0.0.1:8790
```

## 从零复刻完整路线（写给人类，也写给 AI agent）

以下每步都给出命令与**验证点**。数据源清单、各源许可与题量见 `docs/SOURCES.md`；
字段定义见 `docs/SCHEMA.md`；平台坑详解见 `docs/RUNBOOK.md`。

### Phase 0 — 环境与通路

1. 装 uv、开启 WSL2（Windows 才需要）。国内网络：HF 主站不通时 `HF_ENDPOINT=https://hf-mirror.com`、
   `HF_HUB_DISABLE_XET=1`（镜像站不支持 Xet 协议）；GitHub 走 codeload tarball 通道
   （`tools/fetch_tarballs.sh`）。
2. **验证点**：`uv run python -c "import tools.env"` 无报错；`wsl echo ok` 正常。

### Phase 1 — 数据层入库

```bash
uv run python tools/download_datasets.py      # 19 源 HF 数据集（幂等，断点续拉）
bash tools/clone_repos.sh                     # 各基准 harness 仓库
bash tools/fetch_tarballs.sh                  # GitHub tarball 备用通道
uv run python tools/build_index.py            # 统一 schema 入库 + 金标封存
uv run python tools/cutoff_filter.py          # created_at vs 模型 cutoff → per-model 有效性
uv run python tools/export_views.py           # vintage/难度/域/per-model 视图
```

导入器在 `tools/importers/`（swebench 系 / defects4j / 仓库型 / terminal-bench2）。
Docker 依赖的源（SWE-rebench/SWE-smith 等 12.5 万题）入索引后标记休眠，
待镜像按需拉取（`tools/pull_tasks.py` + `tools/image_cache.py`，LRU 上限自定）。
**验证点**：`bank/index.parquet` 行数与 SOURCES.md 题量总和对得上；
`bank/seals/` 文件数 ≥ 有金标的题数；`ls bank/views/` 出视图。

### Phase 2 — 判分环境（分域准备）

- **算法（LiveCodeBench）**：零环境，输出归一化比对（whitespace/字面量/数值/行级等价，
  命中非严格等价记 `score.normalized` 留痕）。
- **Java 修码（Defects4J）**：`bash tools/d4j_env.sh` 初始化；需要便携 JDK11（Adoptium）+
  D4J 自带 ant + gitpan Perl 模块（vendor/，`tools/clone_repos.sh` 会拉）。
  路径必须纯 ASCII（中文路径让 java/ant 静默失败）。
- **Python 修码（BugsInPy）/ 性能（SWE-Perf）**：WSL 内建 py3.8 隔离 venv
  （`scoring.bip_venv`，首次每项目 10-20 分钟）。BIP 的 requirements 是 2020 年
  Windows pip freeze，六类坑的自动修复都在 `harness/scoring.py`（平台包过滤/UTF-16/
  死包容错/setuptools 降级/typing-extensions 恢复/ext4 落盘）。
- 重型工作区根目录默认 `D:/tikutaku`（Windows 下项目树可能被安全软件打 Low 完整性
  标记，树内 exe 起的进程会崩），用环境变量 `TIKUTAKU_DATA_ROOT` 重定向。

**验证点**：`uv run python tools/audit_task.py <bank_id>` 对每域抽 1 题金标预检通过
（应用金标 patch → F2P 全绿）。

### Phase 3 — 金标审计（入正式池的闸门）

上采样标不可信：上游数据集自带 patch 可能不 work、测试可能 flaky。所有题先过机器审计：

```bash
uv run python tools/bip_gold_audit.py <项目名或ID列表>   # BIP：fetch fixed 版→应用金标→跑 F2P
uv run python tools/d4j_gold_audit.py <ID列表>           # D4J：checkout→金标→compile→test
uv run python tools/perf_gold_audit.py <ID列表>          # Perf：金标可编译+基准可跑
```

审计 pass 集回流视图（审计挂 = 金标未验证，不进评测批）。空壳金标、上游残缺的题
retired 但保留数据。**验证点**：审计报告里 pass 率异常低（<80%）先查环境再怀疑数据。

### Phase 4 — 评测与标定

```bash
# 跑批（断点续跑：--skip-done；error:* 判定自动重测）
uv run python harness/run.py --view <view> --adapter api --model <model> \
    --concurrency 4 --thinking-level max --task-timeout 3600
# 第 0 步：离线噪声审计（不调 API）
uv run python tools/audit_scoring_noise.py
uv run python tools/recalibrate_after_audit.py   # 翻案/作废重测留痕
# 难度播种：算法域五档 + 修码/性能域三档 + tier_rank（档内 token 连续序）
uv run python tools/seed_codetask_tiers.py
```

跑批纪律（实测教训）：跑批与交互会话共享账号限流时并发 ≤2；推理模型 max_tokens
拉满（128K）；修码题 task_timeout ≥1800s；长思考链耗尽可能产生空回复——判分器把
空回复记 `error:empty_reply` 而不是 fail，就是为了让它可重测而不是污染难度统计。

### Phase 5 — 组卷与 Web 测试台

```bash
uv run python webapp/server.py    # 或 Windows 双击 启动测试台.bat
```

按域选题 → 总题量 + 五档配比（四种预设/自定义，Σ≠100 自动按比例归一化并明示等效值）
+ 随机种子（可复现）→ 组卷 → 并发评测 → 五档报告 + 历史横向对比。连接三项
（模型名/API Key/接口地址）协议自动探测（OpenAI/Anthropic 兼容）；
**API Key 只在内存中转发，run 目录的 config.json 不落密钥**；结果与 CLI
`harness/run.py` 同构。

## 已知坑速查（血泪换来的，详解见 RUNBOOK）

| 坑 | 解 |
|---|---|
| Windows 项目树被沙箱/安全软件打 Low 完整性标记，树内 exe 起的进程崩 | 生产 venv 与 JDK 放树外干净盘（`TIKUTAKU_DATA_ROOT`） |
| 中文路径 + java/ant | 一律 ASCII 路径 + junction |
| build.xml 的 `file:///d/` URL 缺盘符冒号 | `_fix_d4j_build_xml` 自动补（幂等） |
| wsl.exe 参数层吞 `$?`/`${...}` | WSL 命令一律落 .sh 脚本文件执行，rc 透传 |
| .bat/.ps1 写中文注释 | cmd/PS 按 GBK 解析必炸，一律纯 ASCII |
| pytest-xdist 下 `-x` 首败抛 Interrupted（rc=2） | 按"测试已失败"归类，别记环境错 |
| uv run python 是低完整性 token | 涉 WSL/docker 的操作走 cmd/系统 python |
| hf Xet 协议镜像站不支持 | `HF_HUB_DISABLE_XET=1` |

## 目录结构

```
harness/    run.py（跑批 CLI）· runner_core.py（组卷/池/报告）· scoring.py（四域判分）
            perf_scoring.py · adapters/（api_agent: OpenAI+Anthropic 双协议探测 / cli_agent）
tools/      download_datasets · build_index · seal_gold · audit_task · cutoff_filter
            strip_vcs · export_views · pull_tasks · image_cache（按需拉取+LRU）
            *_gold_audit（三域金标审计）· audit_scoring_noise · recalibrate_after_audit
            seed_codetask_tiers（难度播种）· importers/（四类导入器）· env.py（统一环境）
webapp/     server.py + static/index.html（组卷测试台）
docs/       SCHEMA.md（七组字段）· SOURCES.md（19 源清单+许可）· RUNBOOK.md（操作+坑手册）
bin/        docker.cmd（Windows→WSL docker 包装）
```

## 数据与许可

**本 repo 不分发任何题目数据、金标或模型输出**——请按 `docs/SOURCES.md` 从上游获取并
遵守各自许可（SWE-rebench CC-BY-4.0 需署名；**BugsInPy 无 LICENSE、R2E-Gym 未标注，
仅限本地内部使用不得再分发**；LiveCodeBench 题面 CC 系注明 LeetCode/AtCoder/Codeforces）。
`bank/`、`sources/`、`results/`、`logs/` 已在 `.gitignore` 挡住，防止误提交。

## 安全模型

- 金标 Fernet 加密落盘，密钥 `.key` 单文件，离线备份（丢失=金标全废）
- API Key 运行时从环境或本地配置读，不进 repo、不进 run 目录
- 任务环境剥 VCS 历史，防 agent 翻提交记录抄答案
- 判分器拒绝模型修改测试文件（改测试过关=作弊）

## License

代码 MIT（见 LICENSE）。文档 CC-BY-4.0。各数据源许可见上节——本工厂只提供工具与方法，
数据的合规使用责任在使用者。
