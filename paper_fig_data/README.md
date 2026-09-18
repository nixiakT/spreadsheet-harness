# Paper figure data audit

审计日期：2026-09-17。这里只整理现有产物，没有重新运行实验，也没有修改任何已有结果或论文正文。所有 score/CI 均为百分数。空白不是 0，而是 **missing**。

### 2026-09-18 BASIC 进度补充

截至 **2026-09-18 11:12 CST**，已从原始 per-task `results.json`（包括 `continuation-4w`）补入最新 BASIC 结果；旧行保留，不用新 run 覆盖历史 run：

- V1 DeepSeek：200/200 recorded，197 scored，Soft/Hard = **26.73/21.83**。
- V1 Qwen：200/200 recorded，198 scored，Soft/Hard = **26.60/22.73**。
- V2 Qwen：321/321 recorded，294/297 nonvisual scored，Exact/Modification = **4.42/33.81**；这是完成值。
- V2 DeepSeek 仍在运行。cutoff 时 208/321 recorded、201 nonvisual scored；阶段性 Exact/Modification = **9.95/52.74**。CSV 中 method 明确写为 `SHEETHARNESS-BASIC-PARTIAL@2026-09-18T11:12+08:00`，不得当作 final 或与完整 run 直接排序。其 Template 当时只有 8 个 scored cases，尤其不稳定。

旧 `aggregate_summary.json` 是 continuation 前快照，因此最新 V1 行的 `source_file` 指向原始 worker + continuation task records，而不是引用过期 aggregate。

## 文件与可用性

- `static_plugin_gains.csv`：Bare → Basic → Financial 的绝对分数。最新 Qwen/DeepSeek V1 Basic 已补齐 200 tasks，Qwen V2 Basic 已完成；DeepSeek V2 最新 run 仍以带 cutoff 的 partial 行保存。同一 DeepSeek V1 Financial 配置存在多个版本，且 Qwen V1 Bare 是 912-task full、Basic/Financial 是 200-task subset；因此仍需按 matched subset/run 过滤，**不能无条件把所有行连成三段增益图**。
- `evolution_trajectory.csv`：从 formal paper36 search 的每个 candidate decision 恢复了 12 行逐候选轨迹。可画 General-only / Domain-only / Alternating 的 validation-gate 轨迹，但两个 suite 被同一 7-case gate 联合聚合，**不能画分 suite trajectory**。
- `hd_interaction.csv`：8 个设计占位行（两 suite × 四格），所有观测字段为空。目标 frozen alternating run 的 held-out crossed compositions 尚未执行，故 Table 3/interaction **missing**。
- `backbone_robustness.csv`：已加入完整 Qwen V2 Basic 和带 cutoff 的 DeepSeek V2 Basic partial；DeepSeek/Qwen fixed Codex+core 也可画。Qwen Bare/Financial 的同配置结果仍不全，DeepSeek 最新 Basic 尚未完成，故完整三方法跨-backbone slope **仍不完整**。
- `accuracy_cost_tradeoff.csv`：8 个 V2 点。Harness 点有 score/calls/tokens；Codex/Claude/DSH 点有 wall-clock latency/calls，只有 Codex 有统一 proxy tokens。可以画带缺失编码的探索图，但不是严格 apples-to-apples latency Pareto。

## 字段

`static_plugin_gains.csv`：`backbone` 模型；`benchmark` 数据集版本；`category` 类别；`metric` Soft/Hard/Exact/Modification；`method` 组合；`score` 百分数；`ci_low/high` 95% CI；`n_tasks` 计划任务数；`n_cases` 实际计分数（V1 task 本身包含三 sibling cases，因此留空）；`seed` generation seed；`source_file` 原始证据。

`evolution_trajectory.csv`：`suite` 数据集；这里真实 gate 联合两个 suite，因此写 `Fin-269 + Fin-1.5K`，不伪拆成两行；`strategy` 搜索策略；`round_id/candidate_id` 轮次与候选；`target_group` H/D；`operator_type` 从 generation log 证实为新 skill 合成，记 synthesis；`replay_score`/`transfer_score` 是相应 role 的 exact accuracy，`regression_score` 是 regression role 的 unchanged-cell accuracy；`incumbent_score_*` 与 `candidate_score` 是 gate 明示的 weighted quality：`accuracy + .25*modification_accuracy + .10*regression_accuracy`；gate/reject/decision 为真实 decision JSON；日志没有独立 contract-breach 字段，故留空；`score_split` 写明口径；`n_families=7` 是候选 gate 的 7 个不同 workbook family。

`hd_interaction.csv`：四个 H/D 状态、composition、held-out accuracy/CI、family 数、run id 与来源。当前 8 行均为预注册设计占位，空 accuracy 明确表示 missing，不是 0。

`backbone_robustness.csv`：与静态表相同，但限 V2 category/metric。`n_tasks` 为计划数，`n_cases` 为实际计分数。

`accuracy_cost_tradeoff.csv`：`primary_score` 是 V2 非视觉 297-task Overall Exact、按实际有 official score 的 cases 求均值；latency 是单题 `elapsed_seconds`，calls 是模型请求/turn；tokens 是单题均值（Codex 数字来自下述 proxy 总量除以 297；SHEETHARNESS 来自 task `agent.usage`）；`invalid_output_rate` 对 SHEETHARNESS 行是 `model_execution_failure + not_scored` 占 297，`provider_failure_rate` 是 `scored_after_provider_failure` 占 297。外部 harness 缺少可统一拆分的两种 failure rate，留空。

## 论文表与追溯审计

仓库内未找到论文 `.tex` 或明确的 Table 1/2/3/4 数字清单，故无法逐单元断言“论文正在使用”的版本。最接近当前论文配置且标识最清晰的是：

- Table 1 V1：同一 frozen 200-task subset、seed 41、thinking、50 turns 的 `spreadsheetbench-v1-bare-deepseek...representative-200`、`...basic-1.1-matched...20260915b`、`...financial-1.1-matched...20260915b`。但三者在 scored denominator 上分别 188/190/186，且分数反向下降。
- Table 1 V2：`deepseek-v4-flash-harness-v26-basic-v2-p6-20260915` 与 `...v26-all-v2...` 最接近；两者同 endpoint/config/source snapshot。但 financial-flow audit 已证明 Basic Financial route 有提前结束缺陷（77/100 interaction_turns=1），所以这些只能作为可追溯旧版本，不能标成干净最终结果。
- Table 2/3：`paper36-*qwen36plus-20260917` 是最新 formal search；solver 是 **qwen3.6-plus**、generator 是 `dashscope/glm-5.2`，不是 DeepSeek-V4-Flash。Table 2 held-out evaluation 尚无输出；Table 3 protocol 已冻结但 held-out directory 只有 split/protocol，没有结果。
- Table 4：未找到能唯一映射论文表格的清单。fixed spreadsheet-core 结果来自 `benchmarks/reports/spreadsheetbench-v2-harness-report-20260915.md` 及各 run `results.json`。

## 配置核对

DeepSeek V1 representative 三方法均为 model `DeepSeek-V4-Flash`，thinking=true，max_turns=50，temperature=0，top_p=1，seed=41；subset 文件完全相同。摘要没有 endpoint 字段；启动脚本/同时期 V2 manifest 指向 DashScope relay，但不能仅凭推断回填 endpoint。

DeepSeek V2 Basic/Financial：model alias `dashscope/deepseek-v4-flash`，endpoint `http://47.96.153.159:8010/v1`，thinking=true，reasoning effort medium，max turns/model calls 50，temperature=0，top_p=1，seed=41，official evaluator revision `83d415c`, SHA256 `04a2...59facb0`，297 nonvisual tasks + 24 visualization。CSV 仅聚合非视觉。Basic 295/297 scored；Financial 293/297 scored，含 6 `scored_after_provider_failure`、18 model execution failures、4 not-scored records。

Fixed core report：DeepSeek/Qwen 均 max_turns=50、temperature=0、top_p=1、thinking=true、reasoning effort medium、同 pinned evaluator 和 297 nonvisual task set；报告列 endpoint 为 `http://10.130.138.46:8010/v1`。但 DeepSeek Codex/Claude/DSH 和新的 SHEETHARNESS runs 使用的 endpoint/runner 不同，latency 不严格可比。

Codex proxy 的可靠总量为：DeepSeek prompt 245,405,402、completion 14,084,616；Qwen prompt 51,887,708、completion 870,331（均来自 V2 harness report）。CSV 分别除以统一的 297 nonvisual tasks；Claude/DSH token 字段因记录位置/上游缺失而不混算。

Formal evolution：solver `qwen3.6-plus`，generator `dashscope/glm-5.2`，temperature=0，top_p=1，thinking=true，max_model_calls=50；split seed 20260911；4 development、4 transfer、2 regression、12 sealed heldout tasks，按 source workbook family 分割。每 candidate gate 使用 development 2 + transfer 3 + regression 2 = 7 families。promotion 条件来自 JSON：development exact 不下降、overall modification 不下降、unchanged-cell regression 不下降、transfer exact 不下降、weighted-quality delta > 1e-6。搜索实际 proposal budget：H-only 3，D-only 3，Alternating H round 1 个、D round 5 个；这不是预先声明的统一固定 budget。heldout 在 search-final 中仍为 `heldout_opened=false`。

## Metric aggregation

V1 official Soft：每 instruction 的三个 sibling cases 的平均成功率，再对可评分 instructions 求均值；Hard：三个 sibling 全部成功才为 1，再求均值。摘要中的 `soft_mean_scored/hard_mean_scored` 排除了 not-scored tasks，因此不同 run denominator 有差异。

V2 Exact/Accuracy、Modification Accuracy、Regression Accuracy 由 pinned official evaluator逐 case 输出；CSV 对有 official score 的非视觉 cases 作算术平均。Overall 同样按 case micro-average，不包含 24 个 Visualization tasks。没有将 provider failure 自动当作普通 task failure；未评分 case 不进入 score，但以 `n_tasks/n_cases` 暴露。

## 多版本冲突

- DeepSeek V1 Financial 同一 200 subset 有至少两个完整结果：20260915b 为 Soft/Hard 28.49/23.12（186 scored）；20260916 run2 为 32.46/26.32（190 scored）。两者都保留。后者时间更晚、计分更多，最接近“当前”结果，但没有论文引用证据，不能擅选。
- DeepSeek V1 full Bare 912-task 为 47.67/40.09（858 scored；`spreadsheetbench-v1-bare-deepseek-v4-flash-full-20260914/summary.json`），与 200-task comparable subset 的 46.45/38.30 不同。CSV 为保证三方法 task set 一致使用后者。
- Qwen V1 Bare 是 912-task full，而 Financial 是 200-task subset，不能直接算 gain。Basic 当前仅 189/200 recorded、187 scored，`complete=false`，所以没有输出其分数。
- V2 old Financial runs r16/r18/v26-tools 分别只有 55/86/88 unique scored cases；v26-tools 还含 6 个 source hash pair。它们未与新 full run 拼接，也未 best-of。
- `multi-plugin-coevolution-deepseekflash-20260913/report.json` 有 2-task exploratory factorial，不是同一次 frozen alternating held-out evaluation，未写入 Table 3 CSV。
- `coevolution-matrix-*` 目录存在许多 canary/retry/provider variants，未发现同时满足 frozen H*/D*、两 suite、matched heldout family aggregation的四格结果，未混用。

## Missing 清单与额外检查 A–F

- A：无法完整审计论文 Table 1，因为仓库无论文表原文/引用 manifest；CSV 内每个已采用数字均可追溯。Basic V2 的 11.86/54.53 等可追溯，但受已知路由缺陷影响。
- B：最新 Basic V1 DeepSeek/Qwen 均已通过 continuation 补齐 200/200，分别有 197/198 个 scored cases。最新 Basic V2 Qwen 已完成 321/321；DeepSeek 在 2026-09-18 11:12 CST 的冻结快照为 208/321，仍在续跑。Basic V2 的旧 DeepSeek full 目录虽完成 321 task records，但有 known flow defect，故旧版与新 partial 都保留并清楚区分。
- C：General-only / Domain-only / Alternating 都有真实 validation gate search，但 **没有真实 held-out evaluation**；`heldout_opened=false`。
- D：目标 Table 3 四个 crossed compositions **没有跑全**；heldout pilot 没有结果文件。
- E：有逐 candidate（含 reject/promote）的 evolution history，足够画联合 7-case validation trajectory；没有各 suite 独立逐 round score。
- F：有部分 latency/tokens/calls。Codex proxy tokens可靠；Claude/DSH tokens不可统一；SHEETHARNESS request timings并非完整 wall-clock字段，故 latency留空。可支持带明显 missing/provider 分组的探索图，不支持严格统一环境 Pareto。

## CI / bootstrap

本次没有执行 bootstrap 或二次 CI 统计。原因：静态实验缺少统一 matched task denominator/失败处理规则；Table 3 heldout results 不存在。`paper36-heldout-pilot.../protocol.json` 虽预注册 family bootstrap 10,000 次和 95% confidence，但没有观测结果，不能计算。故所有 CI 留空，interaction `I = S11 - S10 - S01 + S00` 及其 CI 均为 **missing**。简单均值、分位数、failure rate 和 evolution weighted quality 可由 `paper_fig_data/reproduce_aggregates.py` 重现；公式直接写在脚本中，没有随机过程或 seed。

## 来源索引

每行的 `source_file` 是直接来源。聚合审计另参考：`benchmarks/reports/spreadsheetbench-v2-harness-report-20260915.md`、`benchmarks/reports/financial-flow-audit-20260917.md`、各 V2 task `manifest.json`、formal search 的 `split-manifest.json`、`search-final.json`、`state.json`、`decisions/*.json`，以及 heldout pilot 的 `protocol.json` / `pilot-split.json`。
