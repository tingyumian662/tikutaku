# 统一题目 Schema（bank/index.parquet）

每行一题，七组字段。

## identity 身份
| 字段 | 说明 |
|---|---|
| `bank_id` | 本库唯一编号 `前缀-序号`（如 `SWBV-000001`） |
| `source` | 源标识（见 SOURCES.md） |
| `source_id` | 源数据集里的 instance_id |
| `source_url` | 源 issue/PR/公告链接 |

## timing 时间锚点（防污染核心）
| 字段 | 说明 |
|---|---|
| `created_at` | issue/PR 创建日期（ISO）。**判污染的锚**：晚于模型 cutoff 才对该模型有效 |
| `imported_at` | 入库时间 |

## content 内容
| 字段 | 说明 |
|---|---|
| `language` | python/java/cpp/go/rust/ts/js/c#/shell/web/unknown |
| `repo` | `org/repo` 或项目名 |
| `task_type` | bug_fix / perf / security / algo / terminal / feature / refac |
| `problem_statement` | 题面（截断存储，全文见 origin.json） |
| `gold_seal` | 金标加密文件引用（`seals/<bank_id>.bin`，Fernet 加密） |

## verifier 判分依据
| 字段 | 说明 |
|---|---|
| `verifier_type` | f2p_p2p_tests / efficiency_test / oj_tests / reward_script / endtoend |
| `fail_to_pass` | JSON 数组：修复前失败、修复后必须通过的测试 |
| `pass_to_pass` | JSON 数组：修复前后都必须通过的测试（回归检查） |
| `timeout_sec` | 单题时限 |

## environment 运行环境
| 字段 | 说明 |
|---|---|
| `env_type` | docker_image / docker_build / conda / venv / none |
| `image_ref` | 镜像引用（docker_image 时有效） |
| `image_registry` | dockerhub:\<namespace\> / local-build |
| `env_notes` | 环境备注 |

## calibration 难度标定
| 字段 | 说明 |
|---|---|
| `initial_difficulty` | 源数据自带的难度提示（如 LCB 的 easy/medium/hard、Live 的 files=N） |
| `level` | L1~L5（试测后按求解率回填，初值 unknown） |
| `empirical_rate` | 实测求解率（多模型多次运行） |
| `retired` | 退役标记（所有模型全对 → 退役） |

## contamination 污染标记
| 字段 | 说明 |
|---|---|
| `cutoff_ok_json` | `{"模型名": {"ok": bool, "reason": str}}`，cutoff_filter.py 生成 |
| `notes` | 备注（synthetic=合成题天然零污染等） |

## 每题任务目录 bank/tasks/<bank_id>/
```
task.json      索引行（不含 problem_statement 全文）
origin.json    源行完整数据（base_commit/test_patch 等；金标字段已剔除）
problem.md     agent 可读题面
workspace/     （物化后）剥 VCS 的代码快照 + 单提交 git 基线
```
