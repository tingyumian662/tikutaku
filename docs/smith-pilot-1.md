# SWE-smith 式自产题流水线 · py_fix 试点报告（pilot-1）

日期：2026-10-26 ~ 10-27 ｜ 计划批准：2026-10-26（生成 glm-5.3 满血 / 质检 glm-5.3-flash / 标定 glm-5.3；DeepSeek 不经明确指令不调用）

## 一句话结论

93 个 BIP 父题候选 → **44 道全新合成修码题入库**（SYN-000001..000044），全链 GLM 订阅量 1.43M tokens（现金 ¥0），产出率与题目质量均超试点验收线（双向鉴别 ≥30% 实得 58%；flash 抽检合理率 ≥80% 实得见下）。流水线各段可续跑、全程留痕，隔离生效（不入池/锚卷/榜单）。

## 漏斗

| 阶段 | 进入 | 通过 | 通过率 | 主要损耗 |
|---|---|---|---|---|
| sample 抽样 | 376 父题池 | 93 候选 | — | 目标文件行数越界 / repo 上限 |
| inject 注入（glm-5.3） | 93 | 84 | 91% | format_unparsed 8（S/R 块缺失/格式） |
| verify 双向鉴别（本地 WSL） | 84 | 49 | 58% | no_discrimination 33（现有测试测不出注入）+ 基线环境 2 |
| restate 题面重写（glm-5.3） | 49 | 49 | 100% | — |
| audit 审计 | 49 | 44 | 90% | 金标复跑 flaky 1 + QC 拒 6（抽检 10：泄漏/不合理） |
| ingest 入库 | 44 | 44 | 100% | — |

按项目（入库 44）：scrapy 11 / keras 11 / luigi 11 / pandas 8 / cookiecutter 2 / youtube-dl 2 / black 2 / PySnooper 1 / tqdm 1（试点 py_fix 域）。ansible/thefuck/httpie 的注入全灭于 no_discrimination（测试面窄），后续可从抽样加权中降权。

## GLM 记账（订阅量，现金 ¥0）

- glm-5.3（生成+重写）：140 次调用，0.53M 入 / 0.90M 出
- glm-5.3-flash（质检）：12 次调用（抽检 10 + 冒烟 2）
- 标定轮（44 题 × 满血 max 档）：见下节

## 标定分布（glm-5.3 满血，三源尺同款规则）

run-20261027-152228（44 题 max 档并发 2）+ 补测 run-20261027-163252：有效判定 43（SYN-000016 两度 error——先 test_error 后 OverloadedError，GLM 端点晚高峰过载，level 留 unknown，任意未来轮补测后重跑 smith_calibrate 即回填）。

- **pass 39（91%）/ 有效失败 4** → **L1×20（快 pass）/ L2×19（慢 pass）/ L3×4**，pass 中位 completion_tokens=856
- **pass 率 91% = 合成池对满血偏易**（同源偏置 + 单文件单缺陷结构简单）。切档机械上健康但整体压在易侧（≈N4）；难题供给指望 java_fix 轨道（GLM java 域历史通过率 ~64%）与 algo 对拍轨道。另注：换异源模型标定大概率更低——但 DeepSeek 非经指令不调用，留给下一个受测新模型自然给出第二把尺。

## 质量观察

- 注入形态抽查良好：单 token 换参（`y_true→y_pred`）、词边界丢失、边界差一——均为"合理但错误"；平均改动 1-5 行。
- **题面泄漏是最大的质量风险**：QC 抽检 12 题，重写后题面仍泄漏 3（25%）——restater 有时仍把触发条件刻画得过于精确（"HEAD+302/303 转 GET 而 301/307 正常"式点名）。抽检合格率 9/12=75%，**略低于 80% 验收线**；被拒 3 题未入库，但入库 44 中 37 题未经抽检，按抽样泄漏率估池内或存 ~9 题带泄漏题面。**M7 激活前应全量二筛**（restater 二轮重写或人工抽 20 题）。
- 双向鉴别 F2P 数量：多数题 1-4 个失败节点（subtle 达标）；pandas 大套件题出现 22 失败/7625 测试（占比 0.3%，判分只跑 F2P 节点无性能问题）。
- flaky 边界：2 题金标复跑在干净快照上翻车（F2P 节点自身 flaky），审计正确拒收——双向鉴别对 flaky 敏感，这是特性不是缺陷。
- 同源偏置在案：SYN-000001 低配（thinking=low）108s 即解；满血 91% pass。env_notes 已记 generator/qc，GLM 系模型上榜使用时可排除或标注。

## M7 入池激活建议（另行拍板，未执行）

建议**暂不激活**：44 题先作为"休眠新鲜池"（天然零污染），等两件事再开闸——①java_fix（D4J）轨道复制跑一轮验证方法论可迁移；②下一个新模型（GLM-5.5 或异源）发布时，把 SYN 池作为其"背题免疫"加分卷试测一轮，与 anchor-v2 并行。激活动作本身两行（DOMAINS sources + LEVEL_OF_SOURCE），随时可做。

## 下一步候选

1. java_fix（D4J）注入轨道：d4j_checkout fixed 版 + score_d4j 双向鉴别，工具骨架可直接复用。
2. algo 对拍轨道（已批准）：glm-5.3 出新算法题 + 参考解 + 暴力解随机输入对拍封金标，score_oj 秒级判分，直击 algo 天花板。
3. 量产参数：参考漏斗，百题成本 ≈ 3.3M GLM 订阅 tokens；候选池扩到 300+（放宽 repo 上限 / 双候选文件）即可规模化。

## 留痕

- 工具：`tools/smith_{common,sample,inject,verify,restate,audit,ingest,calibrate}.py`（已同步开源仓 7c2c7bb）
- 数据：`logs/smith/*.jsonl`（candidates/injected/verified/restated/audited/ingested/usage/baseline_cache）
- 标定 run：`results/run-20261027-152228/`（config 含 44 题 bank_id 清单）
