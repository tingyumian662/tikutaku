# 数据源清单（题库来源与许可）

## HF 数据集（sources/datasets/）
| 本库源 | HF repo id | 题量 | task_type | 许可 | 署名义务 |
|---|---|---|---|---|---|
| swebench_verified | princeton-nlp/SWE-bench_Verified | 500 | bug_fix | MIT | 无 |
| swebench_full | princeton-nlp/SWE-bench（dev+test，train 已排除） | 2,519 | bug_fix | MIT | 无 |
| swebench_lite | princeton-nlp/SWE-bench_Lite | 300 | bug_fix | MIT | 无（去重后与 full 重合） |
| swebench_multimodal | SWE-bench/SWE-bench_Multimodal | 580 | bug_fix | MIT | 无 |
| swelive_py | SWE-bench-Live/SWE-bench-Live | ~1,888 | bug_fix | MIT | 无 |
| swelive_multilang | SWE-bench-Live/MultiLang | 1,077 | bug_fix | MIT | 无 |
| swelive_windows | SWE-bench-Live/Windows | 66 | bug_fix | MIT | 无 |
| swerebench_v1 | nebius/SWE-rebench | 21,336 | bug_fix | **CC-BY-4.0** | 需署名：SWE-rebench (Nebius) |
| swerebench_v2 | nebius/SWE-rebench-V2 | 32,079 | bug_fix | **CC-BY-4.0** | 需署名：SWE-rebench (Nebius) |
| multiswebench(_mini/_flash) | ByteDance-Seed/Multi-SWE-bench | 1,632+400+300 | bug_fix | CC0 | 无 |
| swesmith | SWE-bench/SWE-smith | 59,136 | bug_fix | MIT | 无（合成题，天然零污染） |
| r2egym_lite | R2E-Gym/R2E-Gym-Lite | 4,578 | bug_fix | **未标注** | ⚠️ 仅限本地内部使用，不得再分发 |
| terminalbench2 | harborframework/terminal-bench-2.0 / -2.1 | 89+89 | terminal | Apache-2.0 | 无 |
| livecodebench | livecodebench/code_generation_lite | ~1,055 | algo | CC（题面） | 注明题源 LeetCode/AtCoder/Codeforces |
| sweperf | SWE-Perf/SWE-Perf | 140 | perf | Apache-2.0 | 无 |

## 仓库克隆（sources/repos/）
| 本库源 | 仓库 | 题量 | task_type | 许可 |
|---|---|---|---|---|
| terminalbench1 | harbor-framework/terminal-bench-1 | ~241 | terminal | Apache-2.0 |
| cvebench | uiuc-kang-lab/cve-bench | 40 | security | Apache-2.0 |
| defects4j | rjust/defects4j（需 init.sh 下载 bundle，Phase 2） | 854 | bug_fix | MIT |
| bugsinpy | soarsmu/BugsInPy | ~493 | bug_fix | **无 LICENSE** ⚠️ 仅限本地内部使用 |
| swelancer | openai/SWELancer-Benchmark @ 999bc21e（main 已清空） | 502 | feature | MIT |

## 关键事实备忘
- Docker 镜像 registry：swerebench/ · swerebenchv2/ · starryzhang/ · mswebench/ · jyangballin/（`__`→`_1776_`）· namanjain12/
- swebench 经典系列无预构建镜像，官方 harness 本地三层构建
- SWE-rebench 教训：任务环境必须剥 git 历史（agent 会翻 git log 抄官方 patch），本库 strip_vcs.py 强制执行
- LiveCodeBench 完整版 `livecodebench/test` 为 gated（自动批准），需 `huggingface-cli login` 后补拉
