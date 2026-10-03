# SpreadsheetBench1.0 on V1 

- 数据集：`benchmarks/data/spreadsheetbench_912_v0.1`
- 固定子集：`benchmarks/results/spreadsheetbench-v1-representative-200-seed42/task_ids.txt`
- 任务数量：200 个唯一 task ID，每个 task 保留全部 test cases
- 并行度：4 个 worker
- 模型：`DeepSeek-V4-Flash`
- `max-turns=50`、`temperature=0.0`、`top_p=1.0`
- 已启用 thinking，reasoning effort 为 `medium`
- 生成种子：`41`
- 协议：V1 先生成 case 1，再把成功的 planner/tool 调用 replay 到 case 2 和 case 3

### 基线结果

| Arm | 输出目录 | 已记录 | 有评分 | 未评分 | Soft 均值 | Hard 均值 |
|---|---|---:|---:|---:|---:|---:|
| Financial | `benchmarks/results/spreadsheetbench-v1-harness-representative-200-seed42-r2` | 200 | 192 | 8 | 0.3056 | 0.2604 |
| Base/Basic | `benchmarks/results/spreadsheetbench-v1-harness-basic-representative-200-seed42` | 200 | 191 | 9 | 0.3019 | 0.2565 |

# SpreadsheetBench1.1 on V1（执行契约修复后重跑）

- 数据集：`benchmarks/data/spreadsheetbench_912_v0.1`
- 固定子集：`benchmarks/results/spreadsheetbench-v1-representative-200-seed42/task_ids.txt`
- 任务数量：200 个唯一 task ID，每个 task 保留全部 test cases
- 并行度：4 个 worker
- 模型：`DeepSeek-V4-Flash`
- `max-turns=50`、`temperature=0.0`、`top_p=1.0`
- 已启用 thinking，reasoning effort 为 `medium`
- 生成种子：`41
- 变更内容：planner fast path 增加 executed/verified 状态及 deterministic post-condition 校验；校验失败时交由 executor 接管
- 两组均记录 200 个唯一 task ID，重复、缺失和额外任务均为 0

| Arm | 输出目录 | 已记录 | 有评分 | 未评分 | Soft 均值 | Hard 均值 |
|---|---|---:|---:|---:|---:|---:|
| Financial | `benchmarks/results/spreadsheetbench-v1-harness-representative-200-seed42-planner-contract-rerun-20260914b` + 补评 | 200 | 180 | 20 | 0.301852 | 0.261111 |
| Base/Basic | `benchmarks/results/spreadsheetbench-v1-harness-basic-representative-200-seed42-planner-contract-rerun-20260914b` + 补评 | 200 | 176 | 24 | 0.314394 | 0.255682 |

Soft/Hard 均值仅以有评分的任务为分母，未评分任务没有计零。本表已按 task_id 逐条重新计算，纠正此前合并均值错误：保留原有评分，仅以成功补评替换原 ProviderError。原始重跑（不含补评）Financial 最后一条记录完成于北京时间 13:44:30，Base/Basic 完成于 14:04:16。逐任务证据见 [配对数据](case1_paired_evidence.json)，Case 1 根因和隔离实验见 [调查与改进方案](case1_investigation_20260914.md)。

### 结果文件

- Financial：`benchmarks/results/spreadsheetbench-v1-harness-representative-200-seed42-planner-contract-rerun-20260914b/aggregate_summary.json`
- Base/Basic：`benchmarks/results/spreadsheetbench-v1-harness-basic-representative-200-seed42-planner-contract-rerun-20260914b/aggregate_summary.json`
- 每个目录下的 `workers/worker-*/results.json` 保存逐任务记录。

# Trace2Skill 正式实验（SpreadsheetBench V1/V2，2026-09-15）

- 统一配置：`max_turns=50`、`temperature=0.0`、`top_p=1.0`、启用 thinking、`seed=41`、4 个 worker（V2 DeepSeek targeted 补跑使用 1 个 worker）
- Agent：`cli_skill_preloaded`
- Skill：Trace2Skill `spreadsheet_agent/skills`（运行时预加载）
- 评分入口：V1 沿用 Trace2Skill 的 `evaluate_with_official.py`；V2 使用仓库中 pinned SpreadsheetBench V2 official comparator，并由 `tools/evaluate_trace2skill_v2_official.py` 适配 Trace2Skill 的全量输出布局。旧 V2 分数因错误回退到 V1 比较器而废弃。
- 说明：启用 `--missing-only` 时，`runner_results.json` 只记录本轮实际运行的 task；被已有完整输出跳过的 task 不在该文件中。下表的“已完成”按当前 output workbook 统计，不把 runner success 当作 benchmark 正确率。

| 实验 | 数据集 | 模型 | 输出目录 | 已完成 task/总数 | 已产出 case/总数 | 当前状态 | 阶段性官方评分 |
|---|---|---|---|---:|---:|---|---|
| V1 DeepSeek | `benchmarks/data/spreadsheetbench_912_v0.1` | `deepseek-v4-flash` | `benchmarks/results/trace2skill-v1-formal-20260913` | 885/912 | 2691/2726 | `--missing-only` 补跑仍在运行；剩余 27 task（2026-09-16 01:01 CST 快照） | 仍为补跑前阶段性评分；待 runner 结束后重新评分 |
| V1 Qwen | `benchmarks/data/spreadsheetbench_912_v0.1` | `dashscope/qwen3-coder-480b-a35b-instruct` | `benchmarks/results/trace2skill-v1-formal-qwen3-coder-480b-20260913` | **800/912** | **2596/2729** | 最近一轮 `--missing-only` 补跑因 DashScope `429 QUOTA_EXHAUSTED` 在处理 112 个缺失 task 时于 `58/112` 中断；当前仍有 112 个 task 未形成完整 3-case 输出 | **最近一次完整评测快照：Fully correct 147/912（16.12%）；Soft 34.58%；Hard 16.12%；case 946/2729（34.66%）** |
| V1 GLM-5.1 | `benchmarks/data/spreadsheetbench_912_v0.1` | `GLM-5.1` | `benchmarks/results/trace2skill-v1-formal-glm-5.1-retry-20260928` | **108/912** | **324/2726** | 当前重跑因上游 deployment cooldown/quota/timeout 中断，已保留输出并准备 `--missing-only` 续跑；剩余 804 task | **阶段性 snapshot：Fully correct 39/108（36.11%）；Soft 50.62%；Hard 36.11%；case 164/324（50.62%）** |
| V2 Qwen | `tmp/trace2skill_spreadsheetbench_v2_full` | `dashscope/qwen3-coder-480b-a35b-instruct` | `benchmarks/results/trace2skill-v2-formal-qwen3-coder-480b-20260914` | 302/321 个有效 XLSX | 302/321 个有效 XLSX | 按用户要求停止继续补跑；剩余 19 task；主要失败原因为 Qwen 429 quota 及未生成 `output.xlsx` | 最新评分文件对应 299-output 快照：DebuggingV2 0.00%，TemplateV2 1.03%，FinancialV2 0.00%；实际 302-output 状态待重评；VisualizationV2 23/24 有效产物、待官方视觉评分 |
| V2 DeepSeek targeted | `tmp/trace2skill_spreadsheetbench_v2_full` | `deepseek-v4-flash` | `benchmarks/results/trace2skill-v2-formal-deepseek-20260914` | 319/321 个有效 XLSX | 319/321 个有效 XLSX | targeted resume 与单 worker retry 均已结束；仅剩 `Template/03_02`、`Template/05_01` 未生成输出 | 当前 319-output 快照已重评：DebuggingV2 0.00%，TemplateV2 18.56%，FinancialV2 19.00%；cell-based Overall Exact 12.46%、Modification 69.07%、Regression 99.19%、Macro 96.67%、Micro 99.36%；VisualizationV2 24/24 待官方视觉评分 |
| V2 GLM-5.1 | `tmp/trace2skill_spreadsheetbench_v2_full_glm51` | `GLM-5.1` | `benchmarks/results/trace2skill-v2-formal-glm-5.1-retry-20260928` | **114/321 个有效 XLSX** | **114/321 个有效 XLSX** | 当前重跑因上游 deployment cooldown/quota/timeout 中断，已保留输出并准备 `--missing-only` 续跑；剩余 183 个 cell-based task，Visualization 尚无输出 | **阶段性 snapshot（114 outputs）：Overall Exact 17/297（5.72%）；Modification 63.29%；Regression 98.79%；Micro cell 97.04%；Visualization 0/24 待官方视觉评分** |

#
### V2 Qwen （2026-09-16 运行状态更新）

- 文件系统当前审计为 `302/321` 个有效 XLSX，仍缺 19 个；单 worker retry 已结束。下表仍对应最近一次已产出的 `eval_official_results_v2.json` 评分快照（其中前三个 cell-based 子集合计 276 个输出，另有 23 个 Visualization 输出），不包含 retry 后新增但尚未重评的 3 个有效 XLSX。

| V2 子集 | 任务数 | 有效输出 | Exact / task accuracy | Modification | Regression | Macro cell | 评分协议 |
|---|---:|---:|---:|---:|---:|---:|---|
| DebuggingV2 | 100 | 83 | 0/100 = 0.00% | 36.00% | 98.92% | 80.42% | V2 cell-based official comparator |
| TemplateV2 | 97 | 93 | 1/97 = 1.03% | 32.90% | 93.62% | 78.89% | V2 cell-based official comparator |
| FinancialV2 | 100 | 95 | 0/100 = 0.00% | 34.17% | 99.52% | 92.19% | V2 cell-based official comparator |
| VisualizationV2 | 24 | 23 | 待 Windows/VLM 官方评分 | 不适用 | 不适用 | 不适用 | Windows Excel/WPS COM + `glm-4.6v` |

三个 cell-based 子集合计：Exact `1/297 = 0.34%`、Modification `34.30%`、Regression `97.31%`、Macro cell `83.89%`、Micro cell `97.22%`。

结果文件：`benchmarks/results/trace2skill-v2-formal-qwen3-coder-480b-20260914/eval_official_results_v2.json`。VisualizationV2 只有在独立官方 Windows/VLM evaluator 完成后才能填写最终分数，不能把待评分任务记为 0。待 19 个残余任务确定最终执行状态后，需要对当前 302-output 状态重新生成评分文件。
<!-- TRACE2SKILL_V2_QWEN_FINAL_END -->

### V2 DeepSeek （2026-09-16 运行状态更新）

- 当前文件审计为 `319/321` 个有效 XLSX、0 个损坏 XLSX，仅缺 `Template/03_02` 和 `Template/05_01`。targeted resume 和后续单 worker retry 都已结束；两项均多轮执行但没有创建目标 `output.xlsx`，目前没有对应 runner 在运行。
- 配置始终保持 `deepseek-v4-flash`、`max_turns=50`、`temperature=0.0`、`top_p=1.0`、thinking on、`seed=41`；精准补跑使用 1 worker，已有成功输出未被覆盖。
- 当前 319-output 状态已经完成独立重算和 pinned 官方 V2 cell comparator 评分。297 个 cell-based task 中 295 个有输出，只有 `Template/03_02` 和 `Template/05_01` 缺失；24 个 Visualization 产物齐全但仍需 Windows/VLM 官方视觉评分。结果文件为 `eval_official_results_v2_current_319.json`。

V2 应按四个官方子集分别报告。下面前三类分数来自当前 319-output 独立重算快照；Visualization 的 24 个输出均已生成且 ZIP/XLSX 有效，但尚未经过官方 Windows 视觉协议，因此不能记为 0，也不能用前三类的 cell accuracy 代替。

| V2 子集 | 任务数 | 当前输出 | Exact / task accuracy | Modification | Regression | Macro cell | 官方状态 |
|---|---:|---:|---:|---:|---:|---:|---|
| DebuggingV2 | 100 | 100 | 0/100 = 0.00% | 48.21% | 99.23% | 97.70% | 当前快照已完成 cell-based 评分 |
| TemplateV2 | 97 | 95 | 18/97 = 18.56% | 79.23% | 98.51% | 93.00% | 当前快照已完成 cell-based 评分；缺 2 个输出 |
| FinancialV2 | 100 | 100 | 19/100 = 19.00% | 80.29% | 99.78% | 99.19% | 当前快照已完成 cell-based 评分 |
| VisualizationV2 | 24 | 当前 24 个有效 XLSX | 待评 | 不适用 | 不适用 | 不适用 | 需 Windows Excel/WPS COM + `glm-4.6v`；VLM score > 0.7 视为通过 |


评分实现的职责分层如下：

- `benchmarks/vendor/spreadsheetbench2-official-83d415c/evaluation/evaluation.py` 是固定在 upstream revision `83d415c...` 的未修改官方比较器；来源及 SHA-256 记录在同目录 `SOURCE.md`。
- `tools/evaluate_trace2skill_v2_official.py` 是本仓库的 CLI/layout adapter：它把 DebuggingV2、TemplateV2、FinancialV2 的 Trace2Skill 输出传给官方 `compare_workbooks_with_regression`，并单独盘点 VisualizationV2 产物为 `pending_official_windows_visual_evaluation`。把这类运维/评分入口放在 `tools/` 符合仓库现有惯例；它不是完整 V2 evaluator，更不是 vendor evaluator 的替代品。
- VisualizationV2 的官方协议固定在 upstream revision `5c16026...`，来源和哈希记录于 `benchmarks/vendor/spreadsheetbench2-visual-official-5c16026/SOURCE.md`。当前 Linux 环境不能产生该项官方分数，最终论文结果需要把 24 个原始输出交给 Windows Excel/WPS COM + `glm-4.6v` evaluator；不得先用 LibreOffice 重算这些文件。
- `tools/score_trace2skill_outputs.py` 是已有的 manifest 驱动适配器，默认服务于旧的 30-task paper-reproduction 清单，任务选择和产物发现约定与本次 321-task formal run 不同，因此没有直接拿它替换全量 V2 专用入口。


### 结果文件

- V1 DeepSeek：`benchmarks/results/trace2skill-v1-formal-20260913/eval_progress_results.json`、`runner_results.json`
- V1 Qwen：`benchmarks/results/trace2skill-v1-formal-qwen3-coder-480b-20260913/eval_official_results.json`、`runner_results.json`
- V2 Qwen：运行状态 `runner_results.json`；错误的历史 V1 口径 `eval_official_results.json`（deprecated）；正确 V2 快照评分 `eval_official_results_v2_recalculated.json`；独立重算副本 `eval_recalculated_outputs_v2_qwen_20260915/`
- V2 DeepSeek：阶段性可信评分 `benchmarks/results/trace2skill-v2-formal-deepseek-20260914/eval_v2_official_recalculated.json`；运行状态 `runner_results.json`；最终将写入 `runner_results_321.json` 和 `eval_official_results_v2_final.json`

# SpreadsheetBench V1 Bare Full（2026-09-14）

- 数据集：`benchmarks/data/spreadsheetbench_912_v0.1`
- 任务数量：912 个唯一 task ID，每个 task 保留全部 3 个 test cases
- 并行度：8 个 worker（每个 worker 使用同一个 V1 benchmark runner）
- Arm：`bare`，composition 为 `runtime-code-interpreter + policy-bare`
- 模型：`dashscope/deepseek-v4-flash`（DeepSeek-V4-Flash）
- CLI 目标配置：`max-turns=50`、`max-model-calls=50`、`temperature=0.0`、`top_p=1.0`、thinking on、`reasoning_effort=medium`；**实际 manifest 未记录/发送 `seed`**（与此前插件实验的 `seed=41` 不一致）
- 协议：先生成 case 1，再将成功的 code/tool 调用 replay 到 case 2 和 case 3；官方 V1 Soft/Hard value-only evaluator
- Bare 路由审计：每个 case 的模型工具 allowlist 仅为 `code_interpreter`，无 workbook profile、skills、verifier、repair、financial runtime、memory 或 evolution

### 实测结果

该节对应两次 Bare 运行。2026-09-14 的 912-task 运行使用了不同 endpoint/model alias 且未设置 seed，只作为历史记录；最终用于与旧插件实验公平比较的是 2026-09-15 严格对齐的 200-task representative rerun。

| 运行 | 输出目录 | 已记录 | completed | not_scored | completion | execution success | Soft 均值 | Hard 均值 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 原始 Full（非对齐） | `benchmarks/results/spreadsheetbench-v1-bare-deepseek-v4-flash-full-20260914` | 912 | 858 | 54 | 94.08% | 93.86%（856/912） | 0.476690 | 0.400932 |
| **严格对齐 rerun（最终）** | `benchmarks/results/spreadsheetbench-v1-bare-deepseek-v4-flash-representative-200-internal-seed41-20260915` | **200** | **188** | **12** | **94.00%** | **96.00%（192/200 scored）** | **0.464539** | **0.382979** |

最终 rerun 的配置为：endpoint=`http://10.130.138.46:8010/v1`，model=`DeepSeek-V4-Flash`，seed=`41`，temperature=`0`，top_p=`1`，thinking on，reasoning=`medium`，max turns/model calls=`50`；任务 ID 完整、无重复/缺失，使用同一 V1 sibling-replay 协议和官方 value-only evaluator。

### 资源与错误（最终 200-task rerun）

- 总 turns：`1,924`（约 9.62 turns/task）；总 tokens：`17,631,733`
- 总 elapsed：`39,735.751` 秒（约 198.68 秒/task）
- 错误：`RenderError=2`、`RecalculationIntegrityError=1`、`JSONDecodeError=1`
- `completed` 与 execution outcome 分开统计：部分 completed 记录仍包含模型执行失败，因此不将 completion 当作 execution success

原始 912-task Full 的资源与错误仍保留在其独立结果目录和日志中；其配置差异见下方“Bare 与插件结果的配置审计”。

### 结果文件

- 最终 200-task 汇总：`benchmarks/results/spreadsheetbench-v1-bare-deepseek-v4-flash-representative-200-internal-seed41-20260915/aggregate_summary.json`
- 最终 200-task worker manifest/results：`benchmarks/results/spreadsheetbench-v1-bare-deepseek-v4-flash-representative-200-internal-seed41-20260915/workers/worker-*/{manifest,results,summary}.json`
- 最终 200-task worker 日志：`benchmarks/results/spreadsheetbench-v1-bare-deepseek-v4-flash-representative-200-internal-seed41-20260915/logs/worker-*.log`
- 原始 912-task Full 汇总及逐任务 artifacts：`benchmarks/results/spreadsheetbench-v1-bare-deepseek-v4-flash-full-20260914/`




























### 同 task 对齐

Bare 与此前 200-task representative subset 有完整 task ID overlap。严格只保留两边都有 score 的 187 个 task 后，Bare 仍高于 Financial：

| 对齐指标 | Bare | Financial | 差值 |
|---|---:|---:|---:|
| Soft | 0.463458 | 0.313725 | +0.149733 |
| Hard | 0.374332 | 0.267380 | +0.106952 |
\\
###

=========

下面的内容暂时不重要

由差异与评分口径

- Bare 的 trajectory 中 `harness.skills.routed` 为 `available=[]/selected=[]`，模型工具 allowlist 只有 `code_interpreter`；它仍会收到核心 agent 的有限 `workbook_first_rows_preview`，这属于 WorkbookSession/read 基线，不是 `profile-deterministic-compact` 的 workbook profile。
- Financial trajectory 会加载 compact profile、选择 spreadsheet skills、先运行 `plan` 阶段，再执行 `code_interpreter`/`recalculate_and_read`，并启用公式验证和 deterministic financial repair。这些是设计中的能力差异，但也会增加 planner 偏航、验证失败和上下文负担；不能用它们解释 provider 不一致造成的差异。
- 两组结果都按“有 score 的 task”计算 Soft/Hard，未评分 task 不补零，所以 Bare 的 `0.476690/0.400932` 不是因为把失败 task 计成高分。Bare 有 858 个 scored task、54 个未评分；此前 Financial 有 192/200 个 scored。正式结果仍应同时报告 completion/execution success。
- Bare Full 的 54 个未评分包含 36 个 output workbook XML 读取失败、10 个 `RenderError`、4 个 `RecalculationIntegrityError`、3 个 `ValueError` 和 1 个 `JSONDecodeError`；这与“所有 task 都异常成功”不符。

### 结论与公平复现实验

本次 Bare Full 是一份有效的 audited-bare 运行记录，但它不是与此前插件结果严格 matched 的 ablation。当前最可疑的实验配置问题是 `base_url + model alias + seed` 三项同时变化；第二层因素是插件自身的 planner/profile/verification/repair 路径可能对模型产生了负面诱导，而不是 evaluator 把 Bare 偏置得更高。

公平比较必须在同一个 endpoint、同一个 model 字符串、同一个 `seed=41`、同一任务 ID 列表和同一生成/预算参数下，用同一个 V1 runner 同时运行 `bare` 与 `spreadsheet-harness-financial`（另跑 basic 也应复用完全相同配置）。建议先用 representative 200-task subset 做 matched rerun，再决定是否重跑 912-task Full；在 matched rerun 完成前，不应把 `0.4767/0.4009` 宣传为 Bare 相对 Ours 的纯能力提升。

### 其他实验的 endpoint 记录

结果目录中的 `manifest.json` 是实际运行配置，不能只看现在已经被修改过的 shell 默认值。按已跑实验归类：

| 实验类别 | 实际 endpoint | 常见 model |
|---|---|---|
| V1 representative 旧 Basic/Financial、planner-contract rerun | `http://10.130.138.46:8010/v1` | `DeepSeek-V4-Flash` |
| 2026-09-11/12 的 DeepSeek harness/codex/core、coevolution、旧 thinking/template/debugging runs | `http://10.130.138.46:8010/v1` | `DeepSeek-V4-Flash`（Pro/GLM 实验也使用同一地址） |
| V1 Bare Full（本次）及 2026-09-13/14 的 financial-thinking/tools/v26 runs | `http://47.96.153.159:8010/v1` | 多数为 `dashscope/deepseek-v4-flash` |
| 2026-09-04--09-08 的 three/four-model、SpreadsheetBench V2、Qwen/GLM/DeepSeek runs | `http://47.96.153.159:8010/v1` | 按实验分别为 `DeepSeek-V4-Flash`、`qwen36-35b-a3b`、`GLM-5.2` 等 |
| Spreadsheet-RL 本地服务 | `http://127.0.0.1:8625/v1` 或 `:8626/v1` | `Spreadsheet-RL-4B` |
| 官方 DeepSeek 直连 canary | `https://api.deepseek.com/v1` | `deepseek-flash` |

特别注意：当前工作树中的 `benchmarks/run_spreadsheetbench_v1_harness_representative.sh` 已被改成默认 `47.96.153.159 + dashscope/deepseek-v4-flash`，但它生成旧 V1 representative 结果时的 manifest 仍明确记录 `10.130.138.46 + DeepSeek-V4-Flash + seed=41`。因此要复现旧结果，必须显式传入旧 endpoint、model 和 seed，不能直接重新执行当前脚本默认值。

### Trace2Skill 四个正式实验的 endpoint

分析中列出的四个 Trace2Skill formal 目录是：V1 DeepSeek、V1 Qwen3-Coder、V2 Qwen3-Coder 和 V2 DeepSeek。它们的 `trace2skill_run_config.json` 没有保存 endpoint 字段；但四个实验均由 `benchmarks/run_trace2skill.sh` 启动，该 launcher 的 `API_BASE_URL` 为 `http://10.130.138.46:8010/v1`，且对应运行历史中的 `OPENAI_BASE_URL` 也为该地址。因此目前能追溯到的实际 endpoint 是：

| Trace2Skill 实验 | endpoint | model |
|---|---|---|
| V1 DeepSeek | `http://10.130.138.46:8010/v1` | `deepseek-v4-flash` |
| V1 Qwen3-Coder | `http://10.130.138.46:8010/v1` | `dashscope/qwen3-coder-480b-a35b-instruct` |
| V2 Qwen3-Coder | `http://10.130.138.46:8010/v1` | `dashscope/qwen3-coder-480b-a35b-instruct` |
| V2 DeepSeek | `http://10.130.138.46:8010/v1` | `deepseek-v4-flash` |

这里的证据强度低于 SpreadsheetHarness 的 manifest（Trace2Skill runner 没有把 URL 写入结果元数据），所以如果要做最终审计，应让 Trace2Skill launcher 将 endpoint、model、seed 和 generation config 一并写入 `trace2skill_run_config.json`，并在 matched rerun 中固定校验。

### Bare endpoint 修正重跑（中止快照，2026-09-15）

此前启动的 Bare 全量重跑使用了正确的 endpoint 和 `seed=41`，但 model 字段仍为 `dashscope/deepseek-v4-flash`，因此不作为最终 matched 结果。因需要改为与旧插件实验完全一致的 `DeepSeek-V4-Flash`，该运行在未完成 912 tasks 前停止。

- 输出目录：`benchmarks/results/spreadsheetbench-v1-bare-deepseek-v4-flash-full-20260915-10_130-seed41-live`
- 中止时记录：280/912 个 task；264 `completed`、16 `not_scored`
- 中止时有效 score：Soft `0.474747`、Hard `0.393939`（仅阶段性观测，不是正式结果）
- 中止时错误：`RenderError` 5、`RecalculationIntegrityError` 3、`JSONDecodeError` 1
- 配置快照：endpoint `http://10.130.138.46:8010/v1`、model `dashscope/deepseek-v4-flash`、seed `41`、temperature `0`、top_p `1`、thinking on、reasoning `medium`、max turns/calls `50`、max output tokens `8192`
- 该快照只用于保留实验记录；正式对照将改用 `DeepSeek-V4-Flash`，并仅跑 representative 200-task subset。

### Bare matched model rerun（200 tasks，2026-09-15）

已按旧 SpreadsheetBench V1 Basic/Financial 实验对齐 endpoint、model 字段和 seed，仅保留 Bare composition：

- 输出目录：`benchmarks/results/spreadsheetbench-v1-bare-deepseek-v4-flash-representative-200-internal-seed41-20260915`
- endpoint：`http://10.130.138.46:8010/v1`
- model：`DeepSeek-V4-Flash`
- seed：`41`；temperature=`0`；top_p=`1`；thinking on；reasoning=`medium`
- 预算：max model calls=`50`、max turns=`50`、max total tokens=`10000000`、max output tokens=`8192`
- 数据集 revision、V1 sibling replay protocol、官方 evaluator 与旧 representative runs 相同；200 个 task ID 完整、无重复/缺失

最终结果：188 个 task `completed`、12 个 `not_scored`；有效 score task 的 Soft=`0.464539`、Hard=`0.382979`。Execution outcome 分布为 `scored=192`、`not_scored=4`、`model_execution_failure=4`；错误类型为 `RenderError=2`、`RecalculationIntegrityError=1`、`JSONDecodeError=1`。总 turns=`1924`（completed task 平均约 9.82），总 tokens=`17,631,733`，总 elapsed=`39,735.751s`。

与旧插件 representative 结果相比（旧 Financial Soft=`0.305556`、Hard=`0.260417`；旧 Basic Soft=`0.301920`、Hard=`0.256545`），在 endpoint、model 字段、seed 和生成/预算参数都对齐后，Bare 仍明显更高；因此 provider 配置混杂已被排除为主要解释，但 composition 引入的 planner/profile/skills/verifier/repair 路径仍可能造成行为差异。

| Method                  | Backbone                                            | V1 Coverage | V1 Verified | V1 Soft (Cell/Sheet/Overall) | V1 Hard (Cell/Sheet/Overall) | V2 Debugging Exact | V2 Template Exact | V2 Financial Exact | V2 Overall Exact | V2 Visualization |
| ----------------------- | --------------------------------------------------- | ----------- | ----------- | --------- | --------- | -------------------------------------- | -------------------------------------- | -------------------------------------- | ---------------------------------------- | ---------------- |
| Bare                    | DeepSeek-V4-Flash                                   | **912/912 denominator** |             | **46.41 / 51.09 / 48.21** | **37.61 / 43.59 / 39.91** | **100/100; 3.00/47.19/98.15/—** | **97/97; 18.56/77.12/98.92/—** | **100/100; 20.00/82.44/99.93/—** | **297/297 non-Visualization; 13.80/68.83/99.00/—/—** | **no official score** |
|                         | Qwen3-Coder-480B                                    | **168/912 completed; 168 scored**§ |             | **38.10**§ | **32.14**§ | **100/100; 0.00/34.92/98.93/—** | **97/97; 0.00/4.79/98.60/—** | **100/100; 0.00/32.52/99.47/—** | **321/321 outputs; 297/297 non-Visualization scored; 0.00/24.27/99.01/—/—** | **Linux proxy: 13/24 = 54.17%；avg 60.72**¶ |
| Trace2Skill | DeepSeek-V4-Flash | **912/912 denominator** | | **45.19 / 51.19 / 47.50** | **25.13 / 37.32 / 29.82** | **0/100 = 0.00%** | **18/97 = 18.56%** | **19/100 = 19.00%** | **37/297 = 12.46%** | **no official score** |
|                         | Qwen3-Coder-480B                                    | **800/912 valid; 912 evaluated (missing counted failed)**† |             | **34.58**† | **16.12**†  | **88/100 current (86 scored snapshot); 0.00/37.19/99.09/83.68**‡ | **94/97; 1.03/32.55/93.52/79.62**‡ | **97/100 current (96 scored snapshot); 0.00/33.76/99.68/93.13**‡ | **302/321 current; V2 retry stopped; 299 evaluated snapshot; 0.34/34.42/97.40/85.54/97.84**‡ | **Linux proxy: 17/24 = 70.83%；avg 72.67**¶ |
|                         | GLM-5.1                                               | **108/912 complete snapshot**† |             | **50.62**† | **36.11**† | **37/100; 0.00/35.55/98.47/35.11**‡ | **63/97; 15.46/76.87/99.27/61.57**‡ | **14/100; 2.00/75.52/97.46/13.30**‡ | **114/321 outputs; 114/297 cell-based outputs; 5.72/63.29/98.79/36.41/97.04**‡ | **0/24 outputs; pending official score**‡ |
| SpreadsheetAgent | DeepSeek-V4-Flash | **912/912 denominator** | | **36.33 / 29.67 / 33.43** | **30.00 / 25.24 / 27.93** | **0/100 = 0.00%** | **12/97 = 12.77%** | **1/100 = 1.04%** | **13/297 = 4.96%** | **Linux proxy: 16/24 = 66.67%** |
|                         | Qwen3-Coder-480B                                    | **912/912 terminal; 756 scored**† |           | **28.22**† | **23.68**† | **51/100 scored; 0.00/39.73/99.40/—**‡ | **81/97 scored; 0.00/7.38/95.14/—**‡ | **66/100 scored; 0.00/30.43/99.90/—**‡ | **198/297 scored; 0.00/23.40/97.82/—/—**‡ | **Linux proxy: 11/24 = 45.83%; avg 48.34**¶ |
| ExternalHarness / Codex + official `xlsx` skill | DeepSeek-V4-Flash | **910/912 denominator** | | **54.31 / 54.15 / 54.25** | **45.28 / 48.42 / 46.48** | **3/100 = 3.03%** | **22/97 = 22.68%** | **23/100 = 23.00%** | **48/296 = 16.22%** | **no official score** |
| ExternalHarness / Claude Code + official `xlsx` skill | DeepSeek-V4-Flash | **912/912 denominator** | | **56.15 / 53.85 / 55.26** | **46.35 / 48.43 / 47.15** | **2/100 = 2.00%** | **23/97 = 23.71%** | **28/100 = 28.00%** | **53/297 = 17.85%** | **no official score** |
| ExternalHarness / DeepSeekHarness + official `xlsx` skill | DeepSeek-V4-Flash | **912/912 denominator** | | **53.89 / 52.99 / 53.55** | **43.85 / 47.29 / 45.18** | **1/100 = 1.00%** | **21/97 = 21.65%** | **27/100 = 27.00%** | **49/297 = 16.50%** | **no official score** |
| ExternalHarness / Codex + official `xlsx` skill | Qwen3-Coder-480B | **839/912 recorded; 673 scored**† | | **26.60**† | **21.40**† | **75/100; 0.00/31.17/74.53/—**† | **84/97; 0.00/18.34/81.20/—**† | **88/100; 0.00/25.67/87.57/—**† | **263/321; 247/297 non-Visualization scored; 0.00/25.13/81.10/—/—**† | **Linux proxy: 15/24 pending official score**† |
| ExternalHarness / Claude Code + official `xlsx` skill | Qwen3-Coder-480B | **910/912 recorded; 909 scored**† | | **29.96**† | **23.98**† | **99/100; 0.00/40.24/97.65/—**† | **97/97; 4.12/39.42/97.27/—**† | **100/100; 0.00/42.54/99.45/—**† | **320/321; 296/297 non-Visualization scored; 1.35/40.75/98.13/—**† | **Linux proxy: 24/24 pending official score**† |
| ExternalHarness / DeepSeekHarness + official `xlsx` skill | Qwen3-Coder-480B | **905/912 recorded; 903 scored**† | | **36.88**† | **30.79**† | **100/100; 0.00/40.26/99.22/—**† | **97/97; 3.09/37.44/97.82/—**† | **100/100; 2.00/44.91/99.54/—**† | **321/321; 297/297 non-Visualization scored; 1.68/40.90/98.87/—**† | **Linux proxy: 24/24 pending official score**† |
| ExternalHarness / Codex (no skill) | DeepSeek-V4-Flash | **912/912 denominator** | | **52.05 / 54.23 / 52.89** | **42.78 / 48.72 / 45.07** | **2/100 = 2.00%** | **23/97 = 23.71%** | **15/100 = 15.00%** | **40/297 = 13.47%** | **no official score** |
| ExternalHarness / Claude Code (no skill) | DeepSeek-V4-Flash | **912/912 denominator** | | **51.81 / 53.47 / 52.45** | **42.07 / 47.86 / 44.30** | **4/100 = 4.00%** | **23/97 = 23.71%** | **16/100 = 16.00%** | **43/297 = 14.48%** | **no official score** |
| ExternalHarness / DeepSeekHarness (no skill) | DeepSeek-V4-Flash | **912/912 denominator** | | **50.27 / 53.37 / 51.46** | **42.42 / 47.86 / 44.52** | **2/100 = 2.00%** | **23/97 = 23.71%** | **17/100 = 17.00%** | **42/297 = 14.14%** | **no official score** |
| ExternalHarness / Codex (no skill) | Qwen3-Coder-480B | **912/912 recorded; 779 scored**† | | **26.19**† | **21.82**† | **100/100; 0.00/42.68/99.29/—**† | **94/97; 1.03/23.48/92.49/—**† | **98/100; 0.00/28.48/97.02/—**† | **315/321; 292/297 non-Visualization scored; 0.34/31.63/96.30/—/—**† | **Linux proxy: 24/24 pending official score**† |
| ExternalHarness / Claude Code (no skill) | Qwen3-Coder-480B | **911/912 recorded; 908 scored**† | | **29.41**† | **24.67**† | **100/100; 0.00/43.17/99.15/—**† | **97/97; 4.12/40.68/97.51/—**† | **100/100; 0.00/37.35/99.42/—**† | **321/321; 297/297 non-Visualization scored; 1.35/40.39/98.70/—/—**† | **Linux proxy: 24/24 pending official score**† |
| ExternalHarness / DeepSeekHarness (no skill) | Qwen3-Coder-480B | **905/912 recorded; 901 scored**† | | **35.15**† | **30.08**† | **100/100; 0.00/41.85/99.08/—**† | **95/97; 3.09/43.85/94.93/—**† | **86/100; 0.00/30.33/85.35/—**† | **305/321; 281/297 non-Visualization scored; 1.01/38.62/93.10/—/—**† | **Linux proxy: 24/24 pending official score**† |

> ExternalHarness V2 指标已按官方固定分母重算：Debugging=100、Template=97、Financial_Model=100，Overall=297（不含 Visualization）。`valid/total` 和 `scored` 仍保留为覆盖率信息；未取得合法 official score 的任务按 0 纳入 Exact、Modification、Regression。因而这些指标不是“仅在已评分任务上的均值”。
| **Spreadsheet-RL (Tools+NativeHarness；无 RL)** | **DeepSeek-V4-Flash** | **903/912 denominator** | | **50.00 / 41.12 / 45.39** | **41.12 / 39.32 / 40.42** | **100/100; 0.00/39.09/98.68/—** | **97/97; 15.46/70.87/98.32/—** | **100/100; 3.00/37.20/99.99/—** | **297/297 non-Visualization; 6.06/48.83/99.00/—/—** | **no official score** |
|                         | **Qwen3-Coder-480B** | **912/912 processed; 908 scored**† | | **34.65**† | **29.07**† | **100/100; 0.00/35.68/99.03/—** | **97/97; 2.06/27.89/96.65/—** | **100/100; 0.00/36.42/94.52/—** | **297/297 non-Visualization; 0.67/33.39/96.73/—/—** | **— (24 excluded)** |
| **SheetHarness (Ours，Financial)** | **DeepSeek-V4-Flash** | **911/912 denominator** | | **55.71 / 53.56 / 54.88** | **45.18 / 47.01 / 45.88** | **8/100 = 8.00%** | **29/97 = 29.90%** | **34/100 = 34.00%** | **<span style="color:#d62728">71/297 = 23.91%</span>** | **Linux proxy: 17/24 = 70.83%** |
|                         | **Qwen3-Coder-480B** | **167/912 recorded; 167 scored** | | **40.52** | **32.93** | **94/100; 6.38/44.47/99.36/—** | **97/97; 10.31/29.66/97.55/—** | **99/100; 5.05/45.16/99.62/—** | **290/297; 7.24/39.75/98.84/—/—** | **Linux proxy: 12/24 = 50.00%；avg 53.07**¶ |
| **SheetHarness (Ours，Basic)** | **DeepSeek-V4-Flash** | **910/912 denominator** | | **56.29 / 54.51 / 55.60** | **46.69 / 48.72 / 47.47** | **8/100 = 8.00%** | **35/97 = 35.05%** | **17/100 = 17.00%** | **60/297 = 20.20%** | **Linux proxy: 9/24 = 37.50%** |
|                         | **Qwen3-Coder-480B** | **167/912 recorded; 166 scored** | | **38.96** | **33.13** | **92/100; 7.61/46.20/99.40/—** | **97/97; 10.31/29.50/97.38/—** | **98/100; 0.00/37.23/99.44/—** | **287/297; 5.92/37.49/98.73/—/—** | **Linux proxy: 11/24 = 45.83%；avg 50.47**¶ |
| Sheet Agent / SpreadsheetAgent | GLM-5.1 | **634/912 denominator** | | **47.25 / 43.38 / 45.27** | **39.16 / 37.54 / 38.33** | **0/100 = 0.00%** | **13/97 = 13.68%** | **0/100 = 0.00%** | **13/297 = 4.38%** | **no official score** |
| Sheet Agent / Spreadsheet-RL | GLM-5.1 | **884/912 denominator** | | **59.91 / 54.94 / 57.99** | **49.54 / 48.68 / 49.21** | **1/100 = 1.00%** | **21/97 = 21.65%** | **30/100 = 30.00%** | **52/297 = 17.51%** | **no official score** |
| ExternalHarness / Codex | GLM-5.1 | **907/912 denominator** | | **52.21 / 53.68 / 52.77** | **43.19 / 48.14 / 45.09** | **0/100 = 0.00%** | **9/97 = 9.28%** | **18/100 = 18.00%** | **27/297 = 9.09%** | **no official score** |
| ExternalHarness / Claude Code | GLM-5.1 | **838/912 denominator** | | **45.59 / 48.35 / 46.62** | **38.67 / 42.81 / 40.21** | **0/100 = 0.00%** | **10/97 = 10.31%** | **15/100 = 15.00%** | **25/297 = 8.42%** | **no official score** |
| ExternalHarness / DeepSeekHarness | GLM-5.1 | **698/912 denominator** | | **55.30 / 57.64 / 56.30** | **45.86 / 51.84 / 48.42** | **1/100 = 1.00%** | **24/97 = 24.74%** | **23/100 = 23.00%** | **48/297 = 16.16%** | **no official score** |
| ExternalHarness / Codex + official `xlsx` skill | GLM-5.1 | **908/912 denominator** | | **56.35 / 55.30 / 55.95** | **47.41 / 49.86 / 48.35** | **0/100 = 0.00%** | **22/97 = 22.68%** | **26/100 = 26.00%** | **48/297 = 16.16%** | **no official score** |
| ExternalHarness / Claude Code + official `xlsx` skill | GLM-5.1 | **836/912 denominator** | | **54.78 / 53.47 / 54.23** | **45.98 / 48.15 / 46.89** | **1/100 = 1.00%** | **25/97 = 25.77%** | **22/100 = 22.00%** | **48/297 = 16.16%** | **no official score** |
| ExternalHarness / DeepSeekHarness + official `xlsx` skill | GLM-5.1 | **856/912 denominator** | | **60.10 / 56.61 / 58.68** | **49.41 / 50.57 / 49.88** | **3/100 = 3.00%** | **23/97 = 23.71%** | **27/100 = 27.00%** | **53/297 = 17.85%** | **no official score** |
| **SheetHarness (Ours，GLM-5.1)** | **GLM-5.1 Bare** | **899/912 denominator** | | **49.31 / 53.61 / 50.95** | **40.93 / 47.95 / 43.60** | **2/100 = 2.00%** | **19/97 = 19.59%** | **19/100 = 19.00%** | **40/297 = 13.47%** | **no official score** |
|                         | **GLM-5.1 Basic** | **883/912 denominator** | | **61.45 / 57.16 / 59.80** | **51.75 / 51.47 / 51.64** | **8/100 = 8.00%** | **27/97 = 27.84%** | **14/100 = 14.00%** | **49/297 = 16.50%** | **no official score** |
|                         | **GLM-5.1 Financial** | **886/912 denominator** | | **60.16 / 57.79 / 59.26** | **49.82 / 52.07 / 50.68** | **7/100 = 7.00%** | **25/97 = 25.77%** | **27/100 = 27.00%** | **<span style="color:#1f77b4">59/297 = 19.87%</span>** | **no official score** |

### 2026-10-01 更新说明

- 本次统一汇总直接读取 DeepSeek-V4-Flash 与 GLM-5.1 的最新本地 `results.json` 和官方 V2 score 字段；不再以旧论文表格中的 `—` 判断结果不存在。
- Visual 已补入可复用的 Linux/LibreOffice + Qwen3-VL proxy 分数：DeepSeek SpreadsheetAgent `16/24`、Trace2Skill `19/24`、Spreadsheet-RL `13/24`、Codex `19/24`、Claude Code `22/24`、DeepSeekHarness `19/24`、Bare `20/24`、Basic `9/24`、Financial `17/24`。这些是 proxy，不是官方 Windows Excel/WPS 分数。
- GLM-5.1 Visual 新一轮 proxy 由于上游 VLM 返回 502，且部分 GLM 产物缺失，未形成有效分数；相应 Visual 列继续留空，不把失败尝试计为 0。
- 计分口径已改为 false inclusion：非 API/Provider 错误（turn limit、缺少 `solution.py`、运行时失败、evaluator 失败等）直接按 0 计入分母；明确识别为 API/Provider 错误的 task/case 才从分母剔除。API 识别同时检查结果字段和任务级 trajectory/stderr/runner 日志，因此 GLM 的部分 timeout/provider failure 也被剔除；各行的 `denominator` 已反映这一点。
- V1 的 Soft/Hard 按 `Cell-Level Manipulation` 与 `Sheet-Level Manipulation` 拆分，列顺序为 `Cell / Sheet / Overall`。分数仅对有效评分 task 求均值；表中的 `scored/912` 是实际纳入均值的任务数。
- V2 的 Exact/accuracy 仅对有合法 official score 的任务求均值；括号中的 `x/y` 为已评分任务数/该类别任务总数，Overall 排除 Visualization。
- 本次找到并纳入了 GLM-5.1 的 DeepSeekHarness（无 xlsx 与 +xlsx）记录。其 V2 Overall 分别为 `48/232 = 20.69%` 和 `53/277 = 19.13%`。
- GLM-5.1 Visualization 当前只有生成或失败记录，没有正式 Windows Excel/WPS + VLM score，因此不将产物数量当作准确率。2026-10-02 直接调用 `http://47.96.153.159:8010/v1`（未使用代理、temperature=0、top_p=1、reasoning medium、thinking enabled）时，`dashscope/qwen3-vl-235b-a22b-instruct` 无响应超时；未将超时伪记为视觉通过/失败，待该模型部署恢复后再补评。
- Trace2Skill 仍使用其最后一个有效映射快照（V1 108-task、V2 114-output）；没有重新调用模型或重跑实验。

- V1 更正（历史说明）：DeepSeek 主结果仍保留原有 audited 全量口径；GLM-5.1 行已在 2026-10-01 更新为本地最新逐 task 统计，不再沿用本条中的 pending 状态。

- DeepSeek v2 的 Ours 行已替换为最新 control16 + near-miss 去重结果；当前只报告 Exact，不再报告 Modification。Financial arm 的 Financial_Model 为严格确认的 `34/100 = 34.00%`；`11_04` 的 evaluator 字段与 runner 状态冲突，未计入。
- Qwen 行保持原值不动；GLM-5.1 的六个 harness baseline 与三个 Ours arm 已补入最新本地统计。GLM-5.1 V2 未评分任务不补零，覆盖率在表中显式给出。
- V1 的六个值按任务 `instruction_type` 拆分为 `Soft: Cell-Level / Sheet-Level / Overall` 和 `Hard: Cell-Level / Sheet-Level / Overall`；本次 GLM-5.1 行已按同一规则从逐 task 结果重算。
- Visualization 已按相同模型、arm、seed=41 配置启动全量评分；完成前只记录 `V2 visual running`，不把未评分视觉任务记为 0。

### 2026-10-02 补跑合并与 Exact 分母口径

- Exact 统一按“实际通过的 task/case 数 ÷ 官方标准总数”计算：Debugging `100`、Template `97`、Financial_Model `100`、V2 Overall `297`（前三类合计，不含 Visualization）、Visualization `24`。因此缺失输出不会改变标准分母，也不会把产物数量当作通过数。
- 已将 GLM-5.1 行中此前使用的阶段性有效输出分母（如 `284/213/225/294/289/285`）改为固定官方分母 `297`；各分类行也统一显示 `100/97/100`。这使 GLM 与 DeepSeek 的 Exact 可以直接比较。
- 已落盘的补跑结果位于 `benchmarks/results/api-retry-20261001/`，会与原始结果按 task ID 去重后相加。截止本次更新，补跑中的 V2 task 尚未产生新的 Exact=1 通过项；因此已有通过数保持不变，补跑目录中的未完成/Provider-error 任务不提前计入最终通过数。
- 当前补跑仍有运行中的 DeepSeek/GLM 进程；待这些进程结束后，只需重新运行同一 task-ID 去重汇总，即可更新分子，不改变上述标准分母。
- 主结果表中 V2 Overall Exact 最高值用红色标记（SheetHarness-DeepSeek Financial，`71/297 = 23.91%`），第二高用蓝色标记（SheetHarness-GLM Financial，`59/297 = 19.87%`）。

### V2 宽松成功判定（Modification > 99% 且 Regression > 99%）

下表不替换上面的官方 Exact 表，而是对相同 V2 逐任务结果增加一套宽松成功判定。`>` 为严格大于，因此恰好等于 99% 不通过。分母沿用各运行实际获得官方 cell-based 分数的任务数；Overall 只合并 Debugging、Template 和 Financial_Model，不包含 Visualization。V1 没有 Modification/Regression 这对指标，故不在本表重复。

| Method | Backbone | Debugging | Template  | Financial  | Overall  |
| --- | --- | ---: | ---: | ---: | ---: |
| Bare | DeepSeek-V4-Flash | **6/100 = 6.00%** | **21/97 = 21.65%** | **21/100 = 21.00%** | **48/297 = 16.16%** |
| Bare | Qwen3-Coder-480B | **3/100 = 3.00%** | **0/97 = 0.00%** | **1/100 = 1.00%** | **4/297 = 1.35%** |
| Trace2Skill | DeepSeek-V4-Flash | **3/100 = 3.00%** | **21/97 = 21.65%** | **37/100 = 37.00%** | **61/297 = 20.54%** |
| Trace2Skill | Qwen3-Coder-480B | **2/100 = 2.00%** | **1/97 = 1.03%** | **2/100 = 2.00%** | **5/297 = 1.68%** |
| SpreadsheetAgent | DeepSeek-V4-Flash | **912/912 denominator** | | **36.33 / 29.67 / 33.43** | **30.00 / 25.24 / 27.93** | **0/100 = 0.00%** | **12/97 = 12.77%** | **1/100 = 1.04%** | **13/297 = 4.96%** | **Linux proxy: 16/24 = 66.67%** |
| SpreadsheetAgent | Qwen3-Coder-480B | **2/38 = 5.26%** | **— (0 scored)** | **— (0 scored)** | **2/38 = 5.26%** |
| ExternalHarness / Codex + official `xlsx` skill | DeepSeek-V4-Flash | **910/912 denominator** | | **54.31 / 54.15 / 54.25** | **45.28 / 48.42 / 46.48** | **3/100 = 3.03%** | **22/97 = 22.68%** | **23/100 = 23.00%** | **48/296 = 16.22%** | **no official score** |
| ExternalHarness / Claude Code + official `xlsx` skill | DeepSeek-V4-Flash | **912/912 denominator** | | **56.15 / 53.85 / 55.26** | **46.35 / 48.43 / 47.15** | **2/100 = 2.00%** | **23/97 = 23.71%** | **28/100 = 28.00%** | **53/297 = 17.85%** | **no official score** |
| ExternalHarness / DeepSeekHarness + official `xlsx` skill | DeepSeek-V4-Flash | **912/912 denominator** | | **53.89 / 52.99 / 53.55** | **43.85 / 47.29 / 45.18** | **1/100 = 1.00%** | **21/97 = 21.65%** | **27/100 = 27.00%** | **49/297 = 16.50%** | **no official score** |
| ExternalHarness / Codex + official `xlsx` skill | Qwen3-Coder-480B | **839/912 recorded; 673 scored**† | | **26.60**† | **21.40**† | **75/100; 0.00/41.56/99.37/—**† | **84/97; 0.00/21.18/93.77/—**† | **88/100; 0.00/29.17/99.51/—**† | **263/321; 247/297 non-Visualization scored; 0.00/30.21/97.52/—/—**† | **Linux proxy: 15/24 pending official score**† |
| ExternalHarness / Claude Code + official `xlsx` skill | Qwen3-Coder-480B | **910/912 recorded; 909 scored**† | | **29.96**† | **23.98**† | **99/100; 0.00/40.65/98.64/—**† | **97/97; 4.12/39.42/97.27/—**† | **100/100; 0.00/42.54/99.45/—**† | **320/321; 296/297 non-Visualization scored; 1.35/40.88/98.47/—**† | **Linux proxy: 24/24 pending official score**† |
| ExternalHarness / DeepSeekHarness + official `xlsx` skill | Qwen3-Coder-480B | **905/912 recorded; 903 scored**† | | **36.88**† | **30.79**† | **100/100; 0.00/40.26/99.22/—**† | **97/97; 3.09/37.44/97.82/—**† | **100/100; 2.00/44.91/99.54/—**† | **321/321; 297/297 non-Visualization scored; 1.68/40.90/98.87/—**† | **Linux proxy: 24/24 pending official score**† |
| Spreadsheet-RL (Tools+NativeHarness；无 RL) | DeepSeek-V4-Flash | **4/100 = 4.00%** | **17/97 = 17.53%** | **10/100 = 10.00%** | **31/297 = 10.44%** |
| Spreadsheet-RL (Tools+NativeHarness；无 RL) | Qwen3-Coder-480B | **2/100 = 2.00%** | **2/97 = 2.06%** | **1/100 = 1.00%** | **5/297 = 1.68%** |
| SheetHarness (Ours，Financial) | DeepSeek-V4-Flash | **9/99 = 9.09%** | **20/97 = 20.62%** | **33/97 = 34.02%** | **62/293 = 21.16%** |
| SheetHarness (Ours，Financial) | Qwen3-Coder-480B | **6/99 = 6.06%** | **8/97 = 8.25%** | **15/97 = 15.46%** | **29/293 = 9.90%** |
| SheetHarness (Ours，Basic) | DeepSeek-V4-Flash | **9/97 = 9.28%** | **24/91 = 26.37%** | **19/99 = 19.19%** | **52/287 = 18.12%** |
| SheetHarness (Ours，Basic) | Qwen3-Coder-480B | **7/100 = 7.00%** | **8/97 = 8.25%** | **7/97 = 7.22%** | **22/294 = 7.48%** |

全部 18 行均由逐任务 Modification/Regression 重新判定并精确计数。`SheetHarness (Ours，Financial)` 的 DeepSeek 逐题源为 `benchmarks/results/deepseek-v4-flash-harness-v26-all-v2-p6-20260915/tasks/*/results.json`，Qwen 逐题源为 `benchmarks/results/ours-qwen-v2-run2-20260916/**/results.json`；两者重算出的各分类任务数和 M/R 均值均与上方原表完全一致。SpreadsheetAgent 使用原表对应的 2026-09-18 15:20 快照（DeepSeek 19 个、Qwen 38 个 Debugging scored），而不是其后继续运行产生的新任务。

表中 V1 Coverage 为 `有效输出/总任务`，并在可追溯时附上实际进入 evaluator 的 scored 数；这里的 scored 以 `status=completed` 且存在 Soft/Hard 分数为准。Bare DeepSeek V1 full 的 `858/912 completed; 858 scored`、Soft=`47.90`、Hard=`40.33` 是 matched 200-task 与剩余 712-task 的官方 V1 score 合并结果；原始 `outcome_kind` 字段有 36 条 `not_scored` 记录被错误标成 `scored`，因 Soft/Hard 为空，已排除。Bare Qwen 的 `868/912 completed; 903 scored`、Soft=`29.34`、Hard=`24.54` 来自 2026-09-15 全量阶段性结果。Trace2Skill V1 Qwen 的最新已评分快照为 Soft=`34.58`、Hard=`16.12`（2026-09-17）；之后继续补跑时因 DashScope `429 QUOTA_EXHAUSTED` 中断，当前落盘为 `800/912` valid、`2596/2729` cases，不能把未重新评测的当前落盘状态与该评分快照混用。Trace2Skill V1 DeepSeek 的 48.01/30.96 仍是 730-task 旧快照，不能视为当前 885 个有效输出的重评结果。SheetHarness V1 两行来自同一个 200-task representative subset，DeepSeek 为 `190 scored + 10 not_scored`，Qwen 为 `192 scored + 8 not_scored`；Soft/Hard 仅按有评分任务计算，结果目录分别为 `benchmarks/results/ours-deepseek-v1-run2-20260916` 和 `benchmarks/results/ours-qwen-v1-run2-20260916`。V2 子集列先记录有效输出情况，再按 `Exact / Modification / Regression / Macro cell` 顺序记录官方 V2 cell-based comparator 指标；若当前有效输出多于评分快照，会同时标出 `current` 和 `scored snapshot`。V2 Overall 列再按 `Exact / Modification / Regression / Macro cell / Micro cell` 顺序记录合计结果，数值均为百分比。两个 Bare V2 R3 目录都已覆盖全部 321 个任务：297/297 个非 Visualization task 均为 `completed/scored`，24/24 个 Visualization task 均有可读 XLSX 产物。DeepSeek 的旧 `205/297` 是运行中途快照，当前已补齐 Template 97/97；Qwen 的 `0.00` 表示 Exact 为 0，不表示没有输出。两者的 Visualization 仍只完成产物生成，尚未获得 Windows Excel/WPS + `glm-4.6v` 官方视觉分数。SheetHarness DeepSeek 的三个 V2 子集当前共完成 `293/297` 个非 Visualization 样例，Macro 尚未提供，因此记为 `—`；Visualization 也暂留空。SheetHarness Qwen V2 已遍历全部 321 个任务，其中 Debugging、Financial、Template 分别有 `99/100`、`97/100`、`97/97` 个官方评分，共 `293/297`；Exact 按各子集总任务数（缺失评分计失败）计算，Modification/Regression 按有效官方评分计算。其 23 个 Visualization 产物仅代表生成完成，尚未获得官方 Windows/VLM 分数；另有 1 个 Visualization 任务因重算完整性错误未生成有效产物。当前结果文件未提供 cell-level correct/total 计数，故 SheetHarness Qwen 的 Macro/Micro 记为 `—`。`†` 表示该运行仍有未完成任务或 V1 评分只覆盖阶段性结果；`‡` 表示 V2 数值来自尚未覆盖当前全部有效输出的阶段性快照，不能当作最终 321-task 结果；`§` 表示 V1 结果为全量阶段性合并；`§§` 表示 Bare V2 已完成产物生成但 Visualization 仍待官方 Windows/VLM 评分。Visualization 的 `24/24` 或 `23/24` 只表示 XLSX 产物数量，尚未完成 Windows Excel/WPS + `glm-4.6v` 官方视觉评分，不能按 0 分计入。

Trace2Skill GLM-5.1 行是 2026-09-29 的中断快照，结果目录分别为 `benchmarks/results/trace2skill-v1-formal-glm-5.1-retry-20260928` 和 `benchmarks/results/trace2skill-v2-formal-glm-5.1-retry-20260928`。V1 仅在 108 个完整三 case task 上计算 Soft/Hard；V2 的 Exact 与 Macro cell 以官方固定分母 297 计入缺失输出，Modification/Regression 只在当前 114 个有效 cell-based 输出上取均值。V2 原始输出目录使用 `Category__Task`，评分时通过只读 symlink 映射为 evaluator 预期的 `Category_Task`，未修改生成 workbook。两项正在用 `--missing-only` 补跑，因此这些数字只能作为阶段性观测。

`¶` 表示 Visualization 的 Linux 近似评分，不是官方 Windows Excel/WPS + `glm-4.6v` 分数。统一协议为 LibreOffice 7.3 命令行隔离导出 PDF、PyMuPDF 栅格化完整工作表，再由内网 `dashscope/qwen3-vl-235b-a22b-instruct` 按官方 checklist 判定；任务级通过条件与官方相同，为 checklist score `> 0.7`。`x/24` 和百分比使用全部 24 个 Visualization 任务作分母，缺失产物、渲染错误或 VLM 错误均视为 proxy non-pass；`avg` 为 24-task 全分母 checklist 平均分（百分制）。逐任务报告位于 `benchmarks/results/visual-linux-proxy-table-20260919/`、`benchmarks/results/visual-linux-proxy-fixed-20260919/`、`benchmarks/results/visual-linux-proxy-agent-20260919/` 和 `benchmarks/results/visual-linux-proxy-agent-qwen-20260919-task126/`。SpreadsheetAgent-DeepSeek 实际有 18 个 Visualization XLSX，已补做 proxy，为 `11/24 = 45.83%`、avg `48.52`；SpreadsheetAgent-Qwen 已单独补跑 24 个 Visualization 任务并完成 proxy，为 `11/24 = 45.83%`、avg `48.34`。Spreadsheet-RL 明确排除了 24 个 Visualization 任务，且官方 checkpoint 服务当前不可用，因此两行仍保持 `—`，不伪造 proxy 分数。正式论文若要求官方口径，仍须用原始 XLSX 经 Windows Excel/WPS COM + `glm-4.6v` 复评。

此前的“六行全量 false-inclusion”说明是历史快照；本次 2026-10-01 统一审计已进一步从任务级日志中识别并剔除 API/Provider 故障，再将其余失败按 0 计入分母。

六个全量逐题文件均为各运行目录下的 `full-denominator-score-20260918.json`，生成时间为 2026-09-18 15:28（北京时间）；原始 `results.json` 未被覆盖，恢复重放详情保存在同目录的 `rescore-replay-recovery-20260918.json`。逐组原始 scored / false-included 数量依次为：DeepSeek + Codex `894/18`、DeepSeek + Claude Code `901/11`、DeepSeek + DeepSeekHarness `894/18`、Qwen + Codex `828/84`、Qwen + Claude Code `901/11`、Qwen + DeepSeekHarness `876/36`。六组均使用 `spreadsheet-core`、`max-turns=50`、`temperature=0`、`top_p=1`、thinking on、reasoning effort `medium` 和内网 LiteLLM。

这六行的 V2 列已补入完成结果。每个分类的 `valid/total` 表示获得 official cell-based score 的任务数；其后依次为 `Exact / Modification / Regression / Macro cell`。Overall 的首个完成数包含 Visualization 产物，随后另列非 Visualization scored denominator；Overall 指标只聚合有 official score 的非视觉任务，不把缺失任务或待视觉评分任务计零。Visualization 仅报告有效产物数量，仍待独立官方视觉评测。对应结果目录为：

- DeepSeek-V4-Flash + Codex：`benchmarks/results/spreadsheetbench-v1-deepseek-v4-flash-codex-core-full-20260917`
- DeepSeek-V4-Flash + Claude Code：`benchmarks/results/spreadsheetbench-v1-deepseek-v4-flash-claude-core-full-20260917`
- DeepSeek-V4-Flash + DeepSeekHarness：`benchmarks/results/spreadsheetbench-v1-deepseek-v4-flash-dsh-core-full-20260917`
- Qwen3-Coder-480B + Codex：`benchmarks/results/spreadsheetbench-v1-qwen3-coder-480b-codex-core-full-20260917`
- Qwen3-Coder-480B + Claude Code：`benchmarks/results/spreadsheetbench-v1-qwen3-coder-480b-claude-core-full-20260917`
- Qwen3-Coder-480B + DeepSeekHarness：`benchmarks/results/spreadsheetbench-v1-qwen3-coder-480b-dsh-core-full-20260917`

Spreadsheet-RL Tools+NativeHarness 的最新结果快照时间为 2026-09-18 13:12（北京时间），来自 `benchmarks/results/native-recovery-20260917/summary.json`。正式 supervisor 已正常退出且 `all_jobs_processed=true`：DeepSeek 与 Qwen 的 V1 均已处理 `912/912`，分别有 897 和 908 条进入官方 evaluator；Soft/Hard 均只按 scored task 计算，unresolved 未补零或伪造计分。DeepSeek 因仍有 12 条 unresolved 而为 `study_complete=false`，另有 3 条 dataset-invalid；Qwen 有 1 条 unresolved 和相同的 3 条 dataset-invalid。3 条原始空指令 dataset-invalid 为 `55224`、`55457`、`55877`。V1 Hard 通过分布（3/3、2/3、1/3、0/3）分别为：DeepSeek `365/65/81/386`，Qwen `264/53/46/545`。

DeepSeek 的 12 条 V1 unresolved 为：`300-35`（轨迹 JSON 的 `Invalid \uXXXX escape`）、`51899`（非法 OOXML data-table range）、`598-46`（workbook relationships 重复 Id）、`39946`、`44913`、`45181`、`55912`、`56427`、`56637`、`56786`、`59902`（LiteLLM HTTP 429，暂无可用 DeepSeek deployment），以及 `59932`（LiteLLM/Dashscope quota exceeded）。Qwen 唯一的 V1 unresolved 为 `142-32`（LibreOffice 重算后 worksheet identity 变化，触发 `RecalculationIntegrityError`）。

V2 两个模型均已完成 297 个非 Visualization task；DeepSeek 的 Template/Financial/Debugging/Overall 严格 Exact 通过数为 `15/97`、`3/100`、`0/100`、`18/297`，Qwen 为 `2/97`、`0/100`、`0/100`、`2/297`。24 个 Visualization task 不在本次 Linux/LibreOffice proxy 范围内，未计入 Overall；Macro/Micro cell-level 指标在该官方输出中未提供，故表中记为 `—`。这四组结果是 Spreadsheet-RL Tools+NativeHarness 的 Linux/LibreOffice clean-room proxy，并非官方 Windows Excel 环境或 RL checkpoint；结果属于保留已有产物、修复基础设施失败并进行必要重跑后形成的 recovery composite。

Basic 最新结果来自原始 worker records 与 `continuation-4w/tasks/*/results.json` 的 task-id 去重聚合。V1 两个模型均已补齐：DeepSeek 为 200/200 recorded、197 scored，Qwen 为 200/200 recorded、198 scored；Soft/Hard 仅按有分数 task 计算。两个 V2 run 也均已遍历 321/321：DeepSeek 有 287/297 个非 Visualization task 获得官方评分，24 个 Visualization task 中 13 个已生成并等待官方视觉评分、11 个 `not_scored`；Qwen 有 294/297 个非 Visualization task 获得官方评分，Visualization 为 22 个待评、2 个 `not_scored`。DeepSeek V2 的 Overall Exact `13.24` 按 287 个有效评分 task 求均值；若把 10 个非视觉未评分 task 计为失败，则是 `38/297 = 12.79%`。V2 单元格按 `Exact / Modification / Regression / Macro cell`，Overall 按 `Exact / Modification / Regression / Macro cell / Micro cell`；Visualization 产物尚未经过 Windows/VLM 官方视觉评分，不计入 Overall。

SpreadsheetAgent 行已按 `benchmarks/results/spreadsheetagent-clean-room-four-full-r4-20260918` 在 2026-09-18 15:20（Asia/Shanghai）生成的最新快照更新：DeepSeek V1/V2 分别为 26/19 个 scored task，Qwen V1/V2 分别为 57/38 个 scored task。V1 Soft/Hard 和 V2 的 Exact/Modification/Regression 均只在已评分任务上取均值，未评分任务不补零；V2 当前只有 Debugging 产生评分，Template、Financial_Model 和 Visualization 尚未产生终态评分。表中 SpreadsheetAgent-Qwen 的旧论文参考值（41.67/34.65）已移除，因为该论文结果与当前视觉替代模型和执行协议不匹配。V2 单元格顺序为 `Exact / Modification / Regression / Macro cell`，Overall 顺序为 `Exact / Modification / Regression / Macro cell / Micro cell`；`—` 表示当前没有可报告的评分。注意：r4 全量任务仍在运行，本表不是 2466-task 最终结果。
### 2026-10-02 补跑续跑（SpreadsheetBench 方法缺失 case）

- 本节对应表中 SpreadsheetAgent、Trace2Skill、Spreadsheet-RL、ExternalHarness（Codex/Claude Code/DeepSeekHarness，含 `xlsx` skill）以及 SheetHarness Basic/Financial 等方法的补跑队列；不涉及 Fin-1.5K candidate screen。
- 原始结果与 trace 保留在 `benchmarks/results/api-retry-20261001/`，未覆盖。新增补跑目录为 `benchmarks/results/api-retry-20261002-p20/`，任务清单由旧 `all-queue.jsonl` 复制生成，仅包含原先识别出的未完成/API/基础设施失败 case。
- 新调度器最多同时运行 32 个方法组；每个方法组内部 `parallelism/workers=20`，`task-timeout/request-timeout/litellm-timeout/replay-timeout=3600`，并清除代理变量。新增运行会生成新的 manifest、case 输出和 trajectory/trace，完成后按 task id 与本表旧结果合并；旧值与旧 trace 不删除。
- `not_scored` 修复口径：未产生合法 score、summary 显示 `completed < expected`、`errors > 0` 或 provider/transport/infrastructure 失败的 case 均进入补跑；真正已完成且有合法 score 的 case 不重复运行。缺失视觉官方分数仍不伪造为 0。
- 当前新增目录仍在运行中；最终分数、覆盖率和 trace 合并表待本批次完成后追加，以上历史表格数值保持不变。

#### 2026-10-02 18:xx 中间状态核对

- 主结果表中的 18 行仍是上一版去重快照，尚未把本轮补跑的中间结果写入最终分数列；原因是当前补跑仍有大量未终态 case，不能把 `not_scored` 当作 0 或把产物数当作通过数。
- 当前 6 小时批次目录：`benchmarks/results/api-retry-20261002-p20-t6h/`。已落盘的中间结果主要集中在 GLM-5.1 的 Claude/DeepSeekHarness/SheetHarness 子批次；DeepSeekHarness V1 的 80 条失败已定位为 DSH 内层约 1800 秒 timeout，正在用修复后的 DSH timeout 单独重跑。
- 旧 1 小时目录 `benchmarks/results/api-retry-20261002-p20/` 和原始结果均保留，不覆盖。最终更新主表前会按 `model + method + suite + task_id` 去重，并同时合并对应 trajectory/trace 路径。

### 2026-10-02 V1 case 分母统一说明

- V1 也统一采用固定 case 分母：SpreadsheetBench v1 共 `912` 个 task、每个 task 3 个 case，因此 Overall 分母为 `2736` cases；Cell/Sheet 子集分别按其对应 instruction-type task 数乘以 3 计算。
- V1 的通过率应按“实际成功 case 数 / 对应标准总 case 数”计算；缺失输出、未评分、运行时失败和 provider/API 失败均不计入成功数，且不改变标准分母。
- SpreadsheetAgent、Trace2Skill、Spreadsheet-RL 的 DeepSeek/GLM V1 补跑结果已纳入同一合并队列 `benchmarks/results/api-retry-20261002-p20/`；该队列当前仍有残余进程，最终 case 计数需在进程结束后按 task ID 去重再写回本表与论文主结果表。
- 同一规则适用于 V2：SpreadsheetAgent、Trace2Skill、Spreadsheet-RL，以及 GLM-5.1 的 ExternalHarness/Codex、Claude Code、DeepSeekHarness（含 `xlsx` skill 和无 skill）均必须按 Debugging=100、Template=97、Financial_Model=100、Overall=297 的固定分母重算；当前论文中的 `52.21/53.68/52.77/.../9.09` 等 GLM V2 行仍是旧快照，尚未完成本轮补跑合并，不能视为最终值。
- 当前已落盘中间快照检查显示：上述 GLM 外部 Harness V2 补跑记录暂未新增 `Exact=1` 的 task，因此按固定分母重算后这些行的 Exact 分子暂不变化；SpreadsheetAgent、Trace2Skill、Spreadsheet-RL 也先保持已有成功数。待补跑结束后再执行最终 task-ID 去重合并。

### 2026-10-03 Fin-1.5K 自进化规模实验：SpreadsheetBench V2 转移结果

本表比较原始 DeepSeek-V4-Flash、静态 SheetHarness，以及仅使用 Fin-1.5K development traces 产生的 50/200/500-task 自进化候选。所有 Exact 均使用 SpreadsheetBench V2 非视觉全量固定分母 `297`；`Scored coverage` 仅用于披露有效 official evaluator 覆盖率，不改变 Exact 分母。自进化候选未读取 SpreadsheetBench 分数作为生成证据。

| Method / evolution evidence | Route | Debugging Exact | Template Exact | Financial Exact | Overall Exact | Scored coverage D/T/F/O | Status |
|---|---|---:|---:|---:|---:|---:|---|
| DeepSeek-V4-Flash Bare | — | 3/100 = 3.00% | 18/97 = 18.56% | 20/100 = 20.00% | 41/297 = 13.80% | 100/97/100/297 | Complete |
| SheetHarness-Basic (static) | — | 8/100 = 8.00% | 35/97 = 36.08% | 17/100 = 17.00% | 60/297 = 20.20% | 97/91/99/287 | Audited snapshot |
| SheetHarness-Financial (static) | — | 8/100 = 8.00% | 29/97 = 29.90% | **34/100 = 34.00%** | **71/297 = 23.91%** | 99/97/97/293 | Audited snapshot |
| Fin-1.5K evolution, 50 traces | D-only | 8/100 = 8.00% | 27/97 = 27.84% | 27/100 = 27.00% | 62/297 = 20.88% | 98/96/92/286 | 11 timeouts remain |
| Fin-1.5K evolution, 50 traces | H-only | 8/100 = 8.00% | 26/97 = 26.80% | 20/100 = 20.00% | 54/297 = 18.18% | 97/97/100/294 | 3 timeouts remain |
| Fin-1.5K evolution, 50 traces | Joint | 8/100 = 8.00% | 26/97 = 26.80% | 28/100 = 28.00% | 62/297 = 20.88% | 97/97/94/288 | 9 timeouts remain |
| Fin-1.5K evolution, 200 traces | D-only | 8/100 = 8.00% | 29/97 = 29.90% | 29/100 = 29.00% | 66/297 = 22.22% | 99/97/92/288 | 9 timeouts remain |
| Fin-1.5K evolution, 200 traces | H-only | 8/100 = 8.00% | 30/97 = 30.93% | 18/100 = 18.00% | 56/297 = 18.86% | 97/97/98/292 | 5 timeouts remain |
| Fin-1.5K evolution, 200 traces | Joint | **9/100 = 9.00%** | 28/97 = 28.87% | 29/100 = 29.00% | 66/297 = 22.22% | 98/97/94/289 | 8 timeouts remain |
| Fin-1.5K evolution, 500 traces | D-only | 7/100 = 7.00% | 15/97 = 15.46% | 11/100 = 11.00% | 33/297 = 11.11% | 75/29/64/168 | **Interim**, resume running |
| Fin-1.5K evolution, 500 traces | H-only | 7/100 = 7.00% | 14/97 = 14.43% | 0/100 = 0.00% | 21/297 = 7.07% | 76/26/83/185 | **Interim**, resume running |
| Fin-1.5K evolution, 500 traces | Joint | 7/100 = 7.00% | 15/97 = 15.46% | 11/100 = 11.00% | 33/297 = 11.11% | 68/25/60/153 | **Interim**, resume running |
| ExternalHarness / Codex + financial-skills-only | — | — | — | — | — | 0/0/0/0 (6 dirs) | **Incomplete; not reportable** |

解读：在当前可审计快照中，200-trace D-only/Joint 达到 `66/297 = 22.22%`，比 Bare 高 `8.42` 个百分点，并接近静态 Financial 的 `23.91%`；50-trace D-only/Joint 为 `20.88%`。H-only 在两个规模上较弱，说明改进并非仅由增加通用提示内容得到，而与 domain/joint mutation route 有关。500-trace 三行仍有大量 provider-timeout/未终态任务，表中百分比是按固定分母计算的当前下界，不能据此声称规模增大导致性能下降。其恢复任务当前按每个机制 `parallelism=20`、`task-timeout=21600`、`request-timeout=1800`、`litellm-timeout=1800` 运行。

Codex + financial-skills-only 的 DeepSeek V2 目录目前只有 6 个 task 目录，且没有聚合 `results.json` 或合法 scored task，因此没有跑完。本表不使用其极小样本估计准确率。注意它不同于已经完成度更高的 Codex + official `xlsx` skill 基线；后者在上方主表中为 `48/296 = 16.22%`。

### 2026-10-02 当前快照回写说明

- Ours-DeepSeek Basic 的 V2 Exact 已按固定分母纠正为 `8/100 + 35/97 + 17/100 = 60/297 = 20.20%`；Financial 仍为 `71/297 = 23.91%`，因此主表中分别标为第二高和最高。
- GLM-5.1 Claude Code 的 V2 Overall 已按固定分母纠正为 `25/297 = 8.42%`。
- GLM-5.1 的三组 Ours Visual 已执行 Linux/LibreOffice + Qwen3-VL proxy，但当前 24 个任务均因上游 VLM `502` 失败（不是视觉质量得分）；因此主表保留 `--`，不把基础设施错误伪造成视觉通过率。对应原始报告在 `benchmarks/results/visual-linux-proxy-glm51-20261001/`。
