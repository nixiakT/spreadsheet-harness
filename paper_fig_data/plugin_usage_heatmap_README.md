# Plugin Usage Heatmap 数据审计

审计日期：2026-09-17/18。没有重新运行实验；统计脚本只读现有 trajectory。主数据为 `paper_fig_data/plugin_usage_heatmap.csv`，可复算脚本为 `paper_fig_data/audit_plugin_usage.py`。Qwen BASIC 补跑在审计时仍在后台进行，因此该部分固定为 **2026-09-17T15:57:58Z** 的快照；脚本内置同一 cutoff，日后复算不会静默纳入新完成的任务。

## 结论与推荐

主图推荐使用 **`task_activation_rate`**。理由是 routed skill、runtime provider、profile hook、control policy 和 verifier 的可审计调用单位不同；“某任务是否真实触发过”在这些插件类型间最可比。`avg_calls_per_task` 适合作为补充图或注释，尤其适合展示 runtime/verifier 强度。`call_share` 可复算且每个 method/category 内约为 100%，但会被高频 `tool.called` 的 runtime provider 主导，不建议作为主文热图颜色。

`recommended_heatmap_plugins`：

1. `runtime-code-plus-formula-validation`
2. `profile-deterministic-compact`
3. `policy-ours`
4. `skill-spreadsheet-structure`
5. `skill-spreadsheet-formula`
6. `skill-spreadsheet-manipulation`
7. `skill-spreadsheet-analysis`
8. `skill-spreadsheet-verification`
9. `verifier-formula-runtime`
10. `skill-spreadsheet-financial-model`

未推荐 `skill-spreadsheet-memory`、`skill-spreadsheet-visualization` 和 `repair-date-text`，因为本批 trajectory 中没有可审计的实际调用事件；它们仍以 0 保留在有 trace 的 CSV 分组中，不能解释成插件不存在。Qwen BASIC 没有 trace 的 category 则所有 usage 字段留空，不能画成 0。

## 数据来源与可比性

| Backbone / method | 原始 run 与 trajectory | 覆盖状态 |
| --- | --- | --- |
| DeepSeek / BASIC | `benchmarks/results/deepseek-v4-flash-harness-v26-basic-v2-p6-20260915/tasks/*/runs/*/*/ours/trajectory.jsonl` | 321/321，完整 |
| DeepSeek / FINANCIAL | `benchmarks/results/deepseek-v4-flash-harness-v26-all-v2-p6-20260915/tasks/*/runs/*/*/ours/trajectory.jsonl` | 321/321，完整 |
| Qwen / BASIC | `benchmarks/results/ours-basic-qwen-v2-20260917/**/trajectory.jsonl` | cutoff 时 127/321，进行中 |
| Qwen / FINANCIAL | `benchmarks/results/ours-qwen-v2-run2-20260916/**/trajectory.jsonl` | 321/321，完整 |

Qwen FINANCIAL 的完成任务位于名字遗留为 `minimax-m2.7/` 的容器目录，但 323 个 materialized `results.json` 的 `model` 均为 `dashscope/qwen3-coder-480b-a35b-instruct`；目录名不作为模型判定依据。该 run 有 324 个原始 trajectory attempts、323 个 materialized attempts、321 个唯一 task；原始重复 task 为 `Debugging/06_09`、`Financial_Model/04_01`、`Financial_Model/05_02`，其中一个 interrupted attempt 没有 `results.json`。脚本只纳入具有同 attempt `results.json` 的 trajectory，并按 `(started_at, path)` 选择每个 task 时间上最后一次的 attempt，不做 best-of。Qwen BASIC cutoff 内为 127 个 materialized、127 个唯一 task。

关键配置：

| Backbone / method | model alias | endpoint | thinking | max turns/calls | temp / top_p | seed | evaluator |
| --- | --- | --- | --- | ---: | --- | --- | --- |
| DeepSeek BASIC/FINANCIAL | `dashscope/deepseek-v4-flash` | `http://47.96.153.159:8010/v1` | true | 50 / 50 | 0 / 1 | 41 | official rev `83d415c`, SHA `04a2a75...facb0` |
| Qwen BASIC | `dashscope/qwen3-coder-480b-a35b-instruct` | `http://10.130.138.46:8010/v1` | true | 50 / 50 | 0 / 1 | 41 | 同上 |
| Qwen FINANCIAL | `dashscope/qwen3-coder-480b-a35b-instruct` | `http://10.130.138.46:8010/v1` | true | 50 / 50 | 0 / 1 | **manifest 未记录** | 同上 |

四组均为 chat-completions、reasoning effort=medium、相同 pinned V2 evaluator，资源上限为 50 model calls、50 turns、10,000,000 tokens、21,600 秒。DeepSeek 与 Qwen endpoint 不同，因此调用模式可按 backbone 分面比较，但不要把 latency 解释成严格 apples-to-apples。Qwen BASIC 与 FINANCIAL 的 backbone、endpoint、thinking、temperature、top_p 和预算一致；seed 只有 BASIC manifest 可核实，且 coverage 严重不对称，所以目前不能做完整的 Qwen BASIC-vs-FINANCIAL 四类别总体比较。

BASIC composition SHA256 为 `f4610d...45c31`（12 plugins）；FINANCIAL 为 `b5d351...4c4e`（13 plugins）。两 composition 只相差 `skill-spreadsheet-financial-model`，DeepSeek/Qwen 使用相同的两个 frozen composition hash，没有混入 composition variant。不过此前审计已确认 BASIC 的 Financial route 存在提前结束缺陷；这会影响调用强度和性能，不能把所有调用差异都解释为 domain plugin 的纯因果效应。

## 什么算“实际调用”

明确排除了 `harness.plugin.activated`。源码显示该事件是在 composition resolve 后对所有 loaded plugins 无条件各写一次，所以它只证明“已加载”，不证明运行时使用。

采用以下保守映射：

| 真实插件名 | 可审计调用信号 | 每次信号计数 |
| --- | --- | ---: |
| `runtime-code-plus-formula-validation` | `tool.called`；composition provider 将 spreadsheet action 与 recalculate/read 路由到该 runtime | 1 |
| `profile-deterministic-compact` | `preprocess.profile` | 1 |
| `policy-ours` | `agent.started` | 1 |
| `skill-spreadsheet-*` | 名字出现在 `harness.skills.routed.payload.selected` | 1 |
| `verifier-formula-runtime` | `agent.formula_runtime_validation_passed/failed/incomplete` | 1 |
| `repair-date-text` | `postprocess.date_text_repair`；仅实际改单元格时才产生 | 1 |
| `skill-spreadsheet-financial-model` | 被 `skills.routed.selected` 选择，或产生受 `financial_model_runtime` 开关保护的 `harness.financial_domain_runtime.warm_started` | 每个信号 1 |

`tool.called` 中的 `code_interpreter`、`view_xlsx`、`recalculate_and_read`、`bash` 在当前 composition contract 下都由同一个 runtime provider 暴露，日志没有更细的“内部插件” dispatch，因此不擅自拆成 Observation/Repair 等虚拟插件。

## plugin_name → role / group

| plugin_name | role | group | 理由 |
| --- | --- | --- | --- |
| `runtime-code-plus-formula-validation` | Act | H | 执行工具 provider；其验证部分由独立 verifier 行表示 |
| `profile-deterministic-compact` | Observe | H | 生成 workbook profile/context |
| `policy-ours` | Control | H | solve/debug routing 与 agent policy |
| `skill-spreadsheet-structure` | Observe | H | 结构理解 |
| `skill-spreadsheet-formula` | Knowledge | H | 通用公式知识 |
| `skill-spreadsheet-manipulation` | Act | H | workbook 操作知识 |
| `skill-spreadsheet-analysis` | Observe | H | 分析与 grounding |
| `skill-spreadsheet-visualization` | Knowledge | H | 可视化知识；本批无实际 route |
| `skill-spreadsheet-verification` | Verify | H | 验证技能 |
| `skill-spreadsheet-memory` | Knowledge | H | 经验/记忆技能；本批无实际 route |
| `verifier-formula-runtime` | Verify | H | formula runtime gate |
| `repair-date-text` | Repair | H | date-text postprocess repair；本批无实际 mutation event |
| `skill-spreadsheet-financial-model` | Knowledge | D | 唯一 domain specialist |

七类 role 中没有 `Workflow` 行，因为两个 frozen compositions 均不包含或调用 `workflow-paper`。没有为了凑齐角色而编造 workflow 使用。

## 指标定义

- `task_activation_rate = 100 × 至少有一次上述调用信号的可读任务数 / 可读任务数`。
- `avg_calls_per_task = 该插件调用信号总数 / 可读任务数`。
- `call_share = 100 × 该插件调用信号总数 / 同 method/category 所有列入插件的调用信号总数`。
- `n_tasks` 是该类别计划任务数；`n_scored_tasks` 按本任务要求表示成功读取 trace 并计入 usage 统计的任务数，不是 evaluator pass 数。完整分组两者相等；Qwen BASIC 的 partial/missing 分组不相等。

零值只表示“在该分组已有可读 trace，且按上述可审计信号没有实际 invocation”。空值表示没有可用 trace，属于 missing。零值不等于插件未加载，也不排除其文字被其他 prompt 拼接逻辑间接使用；日志粒度不足时不作推断。

## Trace 覆盖

| Backbone / method | Template | Financial Modeling | Debugging | Visualization | Missing/parse failure |
| --- | ---: | ---: | ---: | ---: | ---: |
| DeepSeek / BASIC | 97/97 | 100/100 | 100/100 | 24/24 | 0 |
| DeepSeek / FINANCIAL | 97/97 | 100/100 | 100/100 | 24/24 | 0 |
| Qwen / BASIC（cutoff 快照） | **0/97 missing** | **27/100 partial** | 100/100 | **0/24 missing** | 194 tasks missing；0 parse failures |
| Qwen / FINANCIAL | 97/97 | 100/100 | 100/100 | 24/24 | 0 |

总覆盖为 1,090 个 method-task traces：DeepSeek 642，Qwen 448（BASIC 127 + FINANCIAL 321）。四个完整 run-method 网格本应有 1,284 个 cells，因此缺 194，全部来自尚未完成的 Qwen BASIC。Visualization traces 在 DeepSeek 两组与 Qwen FINANCIAL 完整，但其 official visual evaluation 与非视觉 evaluator 不同；usage 统计本身仍可用。

## 特别检查 A–F

- **A — Composition：**已证实不同，FINANCIAL 精确增加 `skill-spreadsheet-financial-model`，其余 12 个插件相同；这一点在 DeepSeek 与 Qwen composition hash 上一致。
- **B — Domain usage：**两个完整 FINANCIAL run 中，financial plugin 的 routing/warm-start 由确定性前置逻辑决定，统计完全一致：Financial Modeling 为 100% activation、1.58 calls/task；Template 为 81.44%、0.8144 calls/task；Debugging/Visualization 为 0。它在 Financial 类最强，但 router 也会依据 Template instruction 选择该 skill，所以不能称为 Financial-category exclusive。Qwen BASIC 的 27 个 Financial traces 中该插件为 0，符合 BASIC composition 不包含它。
- **C — General usage：**Template 主要路由 Formula、Verification，另有部分 Structure/Manipulation；Debugging 为 Formula 与 Verification 100%，Manipulation 32%。这一结论由 explicit routed event 支持。
- **D — 失败任务：**DeepSeek 非视觉 scored cases 中，formula runtime verifier 的 pass/fail 调用均值为 BASIC 0.829/0.927、FINANCIAL 0.980/1.504；Control 为 BASIC 0.800/1.215、FINANCIAL 0.918/1.439。Qwen 为 BASIC 0.400/3.592（仅 5 pass、120 fail，且 run partial）、FINANCIAL 0.217/3.093（23 pass、270 fail）。Verification skill 差异很小或方向不稳定，date repair 均为 0。失败题通常更长且可能重试，这只是描述性相关，不是因果结论；Qwen BASIC 尤其不能外推到缺失类别。
- **E — Loaded vs invoked：**主 CSV 完全不使用无条件的 `plugin.activated`；composition-only 与 actual invocation 已区分。
- **F — 稀有插件：**Memory、Visualization skill、date repair 为 0；Structure 和 Analysis 仅在部分类型触发。原始统计全部保留，推荐主图排除三个零调用插件。

## 可靠性分级与限制

最可靠的是 explicit `skills.routed.selected`、`tool.called`、`preprocess.profile`、formula validation events 和 financial warm-start event。`policy-ours` 使用 `agent.started` 代理实际 policy invocation；一次任务可能因阶段/恢复出现多次 started，因此 activation rate可靠、calls 强度需谨慎解释。

技能 route 表示该 skill 被选择并放入模型上下文，是运行时 activation，但日志不记录模型是否阅读或遵循其中某一条规则。Memory/Visualization/date-repair 的零调用可能是路由未选择或 no-op hook 没有细粒度事件，不能推断它们“没有任何影响”。

## 是否可用于论文主文

以 `task_activation_rate` 绘制、在图注中写明“explicit routing/provider/event activation”后，**DeepSeek BASIC/FINANCIAL 与 Qwen FINANCIAL 足够用于主文**。Qwen BASIC 目前只可展示完整的 Debugging，以及明确标注 `27/100` 的 Financial Modeling 部分样本；Template/Visualization 必须用 missing mask，不能显示为 0。若主图要求 Qwen 的 BASIC/FINANCIAL × 四类别完整对照，目前数据仍不够。

不建议把 `call_share` 作为主图，也不建议宣称这是模型内部因果贡献或插件内容被实际阅读的概率。BASIC Financial 的 known early-stop defect必须在正文或图注中披露。跨 backbone 的 activation routing 很多由相同确定性规则产生，几乎相同是合理现象；更能反映 backbone 行为差异的是 runtime/verifier 的 `avg_calls_per_task`，但这项更容易受失败率、重试与 provider 状态影响。
