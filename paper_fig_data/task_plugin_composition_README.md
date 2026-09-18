# Task × Plugin Composition Matrix 数据审计

审计日期：2026-09-17。主数据为 `paper_fig_data/task_plugin_composition.csv`。本次只读取已有 run、dataset metadata、`results.json` 和 trajectory；没有重新运行实验，也没有修改已有结果。空白表示 missing，不表示 0。

## 结论

这份 CSV 可直接用于论文主图，但主图应：

- 过滤 `method == SHEETHARNESS-FINANCIAL` 和下方 `recommended_task_ids`；
- 方块使用 `invoked`，不要使用 `loaded`；
- Visualization 的 `final_score` / `task_success` 不画或标为 N/A；
- 图注将 invocation 表述为 “explicit routing/provider/event activation”，不要表述为模型内部因果贡献；
- 披露该 frozen run 是可追溯的旧 V2 run，且 BASIC 的 Financial route 有已知 early-stop defect。因此 BASIC 可作补充对照，不宜据此作调用强度的因果解释。

CSV 是完整长表：每个 method-task 对每个已识别插件一行，包括未加载和未调用的 0 行。共 **8,346 行、642 个 method-task（321 个唯一 benchmark task）、13 个插件、4 个 category**。推荐主图使用 **21 个 FINANCIAL method task**。

## 数据来源与覆盖

两个同配置 DeepSeek-V4-Flash / SpreadsheetBench v2 run：

- `SHEETHARNESS-FINANCIAL`：`benchmarks/results/deepseek-v4-flash-harness-v26-all-v2-p6-20260915`
- `SHEETHARNESS-BASIC`：`benchmarks/results/deepseek-v4-flash-harness-v26-basic-v2-p6-20260915`

两者均为 model alias `dashscope/deepseek-v4-flash`，thinking=true，reasoning effort=medium，temperature=0，top_p=1，seed=41，max model calls/turns=50，并使用相同 pinned V2 evaluator。BASIC composition SHA256 为 `f4610d08...645c31`，含 12 个插件；FINANCIAL 为 `b5d351ff...ce4c4e`，含 13 个插件。唯一 composition 差异是 FINANCIAL 多 `skill-spreadsheet-financial-model`。

| Method | Template | Financial Modeling | Debugging | Visualization | 可读 trace |
| --- | ---: | ---: | ---: | ---: | ---: |
| SHEETHARNESS-BASIC | 97/97 | 100/100 | 100/100 | 24/24 | 321/321 |
| SHEETHARNESS-FINANCIAL | 97/97 | 100/100 | 100/100 | 24/24 | 321/321 |

所有 642 条 trajectory 均可解析，并均含 `harness.composition.resolved`。非视觉 official score 覆盖 BASIC 295/297、FINANCIAL 293/297。Visualization 24/24 有 execution trace，但 frozen output 只有 `pending_official_visual_evaluation` / `not_scored`，没有与非视觉三类可比的 official exact score，故其 624 个 plugin rows 的 `final_score` 和 `task_success` 留空。

完整性分级：Template、Financial Modeling、Debugging 的 invocation trace 完整，且绝大多数有 official score；Visualization invocation trace 完整，但 evaluator outcome 不完整。FINANCIAL 缺 4 个非视觉 score（Financial Modeling 3、Debugging 1）；BASIC 缺 2 个 Debugging score。没有把缺失 score 猜成 0。

## 字段定义

- `task_id`：trajectory/config 中的原始 `Category/item_id`。
- `family_id`：从官方 dataset metadata 的 `spreadsheet_path` 读取 `spreadsheet/` 后的 workbook-family 目录；不是根据 task id 猜测。Visualization 每题目录本身就是 family。Debugging 为 `01_Debugging` 等。
- `loaded`：插件名字出现在该 task 的 `harness.composition.resolved.payload.composition.plugins`。
- `invoked`：至少出现一次下表定义的保守 runtime 调用信号。主图只用这一列。
- `call_count`：该调用信号的出现次数；不同插件的信号单位不同，不应跨插件直接比较强度。
- `first_call_turn` / `last_call_turn`：信号对应的 model turn。composition resolve / preprocessing / routing / warm-start 发生在首个 model request 前，记为 turn 0；`tool.called` 继承最近的 `model.requested.payload.turn`；validation 使用事件自带 turn。未调用留空。
- `final_score`：`results.json.official_score.accuracy`，即 V2 exact/accuracy 主指标，取值 0 或 1；未评分留空。
- `task_success`：有 official accuracy 时严格定义为 `accuracy == 1.0`；未评分留空。
- `invalid_output`：`outcome_kind` 为 `model_execution_failure` 或 `not_scored` 时为 1。Visualization 的正常 `pending_official_visual_evaluation` 为 0；这与“尚无 visual score”是两个概念。
- `source_file`：逐 task 的直接 trajectory 来源。score/outcome 来自同 task directory 的 `results.json`，family 来自相应 category 的 `dataset.json`。

## Invocation 识别

`harness.plugin.activated` 被明确排除：源码和 trace 均表明它在 resolved composition 后对所有 loaded plugins写一次，只能证明 available/loaded。

| plugin_name | invocation 信号 | turn / count 口径 |
| --- | --- | --- |
| `runtime-code-plus-formula-validation` | 每个 `tool.called` | 继承最近 model request turn；每事件 1 次 |
| `profile-deterministic-compact` | `preprocess.profile` | turn 0；每事件 1 次 |
| `policy-ours` | `agent.started` | turn 0；每事件 1 次（恢复/二阶段可多次） |
| `skill-spreadsheet-*` | 名字出现在 `harness.skills.routed.payload.selected` | turn 0；每次选择 1 次；`visual-review` 映射到 visualization plugin |
| `verifier-formula-runtime` | `agent.formula_runtime_validation_passed/failed/incomplete` | 使用事件的 turn；每事件 1 次 |
| `repair-date-text` | `postprocess.date_text_repair` | 仅发生实际 date-text mutation 才计数 |
| `skill-spreadsheet-financial-model` | 显式 skill route，或受 `financial_model_runtime` 开关保护的 `harness.financial_domain_runtime.warm_started` | turn 0；两个独立信号各计 1 次 |

`tool.called` 中的 `code_interpreter`、`view_xlsx`、`recalculate_and_read` 和 `bash` 在这些 compositions 下由同一个 action runtime provider 暴露，日志没有更细的内部插件 dispatch，故没有拆成自创插件。skill route 证明该 skill 被选择并加入执行上下文，不证明模型阅读或采用了其中某条规则。

## plugin_name → role / group

名称和 role 以真实 registry / frozen composition 为准；H/D 沿用论文的 General / Domain 分工。当前 composition 没有 Workflow plugin，因此没有为了凑齐七类而加入 `workflow-paper`。

| plugin_name | role | group |
| --- | --- | --- |
| `runtime-code-plus-formula-validation` | Act | H |
| `profile-deterministic-compact` | Observe | H |
| `policy-ours` | Control | H |
| `skill-spreadsheet-structure` | Observe | H |
| `skill-spreadsheet-formula` | Knowledge | H |
| `skill-spreadsheet-manipulation` | Act | H |
| `skill-spreadsheet-analysis` | Observe | H |
| `skill-spreadsheet-visualization` | Knowledge | H |
| `skill-spreadsheet-verification` | Verify | H |
| `skill-spreadsheet-memory` | Knowledge | H |
| `verifier-formula-runtime` | Verify | H |
| `repair-date-text` | Repair | H |
| `skill-spreadsheet-financial-model` | Knowledge | D |

Repair 和 Workflow 的实际 invoked 值在本批均为 0。visualization knowledge skill 和 memory skill 也均为 0；这些 0 仅表示没有上述可审计调用信号，不等于插件不存在。

## recommended_task_ids

以下 21 行均指 `method = SHEETHARNESS-FINANCIAL`。选择在 category 内覆盖不同 invocation pattern、workbook family、exact success/failure；没有只保留成功题。Visualization 没有 exact success/failure，因此保留 1 个 invalid 和 5 个正常生成但待 visual evaluator 的例子。

| task_id | outcome | 推荐理由 |
| --- | --- | --- |
| `Template/01_04` | success | 最小的 structure + manipulation + analysis 路由；无 runtime/control，展示 preprocessing fast path |
| `Template/08_03` | success | formula + verification + D financial，无 runtime；体现 Template 也可能按 instruction 路由 domain skill |
| `Template/07_01` | failure | structure/manipulation/verification 与 runtime verifier 的失败例，且不调用 D plugin |
| `Template/03_01` | failure | formula + analysis + D financial pattern，与 structure 型 Template 对照 |
| `Template/16_02` | success | Template 主流 formula/verification/D pattern 的成功例 |
| `Financial_Model/05_01` | success | 最小 financial fast path：profile + formula + verification + D plugin |
| `Financial_Model/17_03` | success | manipulation 分支；runtime、control、runtime verifier 与 D plugin 同时调用 |
| `Financial_Model/04_05` | success | analysis 分支；与 manipulation/verification 分支形成组内差异 |
| `Financial_Model/13_03` | success | 最常见 formula + verification + D full-execution pattern |
| `Financial_Model/07_01` | failure | 同样调用 D 与 verification，但没有 runtime verifier 且 exact failure，避免只选成功样本 |
| `Debugging/05_03` | success | no-runtime fast path，同时有 formula/manipulation/verification |
| `Debugging/03_05` | success | 主流 debugging pattern，含 runtime verifier |
| `Debugging/01_07` | success | 与上一行相近但不触发 runtime verifier，展示 Verify 内部差异 |
| `Debugging/07_08` | failure | manipulation + verification + runtime verifier 的失败例 |
| `Debugging/07_01` | failure | manipulation + verification，但无 runtime verifier；与上一行成对 |
| `Visualization/Task 96` | invalid / unscored | 唯一 FINANCIAL visualization invalid case；保留真实失败边界，非 cherry-pick |
| `Visualization/Task 127` | unscored | manipulation + analysis + verification，正常生成待 visual evaluation |
| `Visualization/Task 1423401` | unscored | 上述 pattern 再增加 runtime verifier |
| `Visualization/Task 1422635` | unscored | formula + manipulation + verification + runtime verifier |
| `Visualization/Task 129` | unscored | formula + manipulation + analysis，但不 route verification skill |
| `Visualization/Task 1437004` | unscored | manipulation/verification + runtime verifier，无 formula/analysis |

## 额外统计 A–F

### A. 每个 category 最常激活的插件

下表为 FINANCIAL 主方法的 task activation rate；并列最高全部列出。

| Category | 最常激活插件 | 激活率 |
| --- | --- | ---: |
| Template | `profile-deterministic-compact` | 100% |
| Financial Modeling | `profile-deterministic-compact`; `skill-spreadsheet-formula`; `skill-spreadsheet-financial-model` | 100% |
| Debugging | `profile-deterministic-compact`; `skill-spreadsheet-formula`; `skill-spreadsheet-verification` | 100% |
| Visualization | `runtime-code-plus-formula-validation`; `profile-deterministic-compact`; `policy-ours`; `skill-spreadsheet-manipulation` | 100% |

### B. 每个 category 平均激活插件数

| Category | FINANCIAL | BASIC |
| --- | ---: | ---: |
| Template | 6.742 | 5.928 |
| Financial Modeling | 6.640 | 4.950 |
| Debugging | 5.890 | 5.870 |
| Visualization | 5.917 | 5.833 |

### C. Financial Modeling 是否更常激活 D plugins

是，在 FINANCIAL method 内很明显：D plugin 每 task 平均激活数为 Financial Modeling **1.000**、Template **0.814**、Debugging **0**、Visualization **0**。唯一 D plugin 在 Financial Modeling 为 100/100，在 Template 为 79/97（81.44%）。因此可以说它对 Financial Modeling 最稳定，但不能说它仅用于该 category。BASIC 根本未加载 D plugin，所有 category 的 D invocation 都为 0。

### D. Debugging 是否明显增加 Verify / Repair

相对 Financial Modeling，Debugging 明显增加 **Verify**：FINANCIAL method 平均每 task 激活的 Verify plugins 为 Debugging **1.770**，Financial Modeling **1.360**；verification skill activation 为 100% vs 50%。但相对 Template（1.773）并没有增加。**Repair 不增加**：`repair-date-text` 在所有 category 均无 mutation event，平均为 0。因此证据支持“Debugging 强调 Verify”，不支持“Debugging 增加 Repair”。

### E. 是否真的存在 task-aware 差异

存在。按 invoked plugin set 计，FINANCIAL 的 321 tasks 有 **24** 种 pattern（Template 9、Financial Modeling 8、Debugging 5、Visualization 6；跨 category 有重合）；BASIC 有 **25** 种。举例：FINANCIAL 中 analysis skill activation 为 Template 8.25%、Financial Modeling 35%、Debugging 0%、Visualization 37.5%；manipulation 为 18.56%、15%、32%、100%；D financial plugin 为 81.44%、100%、0%、0%。这不是每题固定调用同一套插件。

### F. resolved composition 与 runtime invocation 差异

差异明显。FINANCIAL 有 4,173 个 loaded task-plugin slots，仅 2,049 个 invoked（49.10%）；BASIC 有 3,852 个 loaded slots，仅 1,797 个 invoked（46.65%）。逐 category 的 invoked/loaded 比例为：

| Category | FINANCIAL | BASIC |
| --- | ---: | ---: |
| Template | 51.86% | 49.40% |
| Financial Modeling | 51.08% | 41.25% |
| Debugging | 45.31% | 48.92% |
| Visualization | 45.51% | 48.61% |

因此 `loaded` 不能代替 runtime invocation；若用 loaded 画主图，FINANCIAL 几乎每行都会固定为 13 格，恰好掩盖 task-aware routing。

## 日志粒度与可靠性

优点是 composition、skill routing、tool calls、profile、formula validation 和 financial warm-start 都有显式事件，且 trace 覆盖完整。局限包括：

- skill route 只证明 selected/context activation，不证明模型实际遵循 skill 内容；
- `policy-ours` 没有独立 dispatch event，以 `agent.started` 为保守 proxy，恢复/二阶段会增加 call count；
- 多种 model-facing tools 统一归属 action runtime，不能再细拆 Observe/Act/Repair；
- date repair no-op 没有 invocation event，所以 0 表示没有实际 mutation 信号；
- turn 0 是本导出的明确约定，表示 model loop 前的调用，不是原 trace 自带 turn；
- Visualization 缺 official visual evaluator score，不能用于 success/failure 分层结论。

在这些限定下，`invoked` 二值矩阵足够可靠用于主文展示 task-aware runtime activation 与 H/D 分工；`call_count` 和跨插件强度比较只适合补充分析。

## 最终汇总

- CSV 行数：**8,346**
- method-task 数：**642**（321 个唯一 task × 2 methods）
- plugin 数：**13**
- category 数：**4**
- 推荐主图 task 数：**21**
- 是否可直接画主图：**可以**，按本文开头的过滤和图注限制使用 `invoked`
