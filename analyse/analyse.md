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
| V2 Qwen | `tmp/trace2skill_spreadsheetbench_v2_full` | `dashscope/qwen3-coder-480b-a35b-instruct` | `benchmarks/results/trace2skill-v2-formal-qwen3-coder-480b-20260914` | 302/321 个有效 XLSX | 302/321 个有效 XLSX | 按用户要求停止继续补跑；剩余 19 task；主要失败原因为 Qwen 429 quota 及未生成 `output.xlsx` | 最新评分文件对应 299-output 快照：DebuggingV2 0.00%，TemplateV2 1.03%，FinancialV2 0.00%；实际 302-output 状态待重评；VisualizationV2 23/24 有效产物、待官方视觉评分 |
| V2 DeepSeek targeted | `tmp/trace2skill_spreadsheetbench_v2_full` | `deepseek-v4-flash` | `benchmarks/results/trace2skill-v2-formal-deepseek-20260914` | 319/321 个有效 XLSX | 319/321 个有效 XLSX | targeted resume 与单 worker retry 均已结束；仅剩 `Template/03_02`、`Template/05_01` 未生成输出 | 当前 319-output 快照已重评：DebuggingV2 0.00%，TemplateV2 18.56%，FinancialV2 19.00%；cell-based Overall Exact 12.46%、Modification 69.07%、Regression 99.19%、Macro 96.67%、Micro 99.36%；VisualizationV2 24/24 待官方视觉评分 |

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

| Method                  | Backbone                                            | V1 Coverage | V1 Verified | V1 Soft   | V1 Hard   | V2 Debugging (valid/total; E/M/R/Macro) | V2 Template (valid/total; E/M/R/Macro) | V2 Financial (valid/total; E/M/R/Macro) | V2 Overall (valid/321; E/M/R/Macro/Micro) | V2 Visualization |
| ----------------------- | --------------------------------------------------- | ----------- | ----------- | --------- | --------- | -------------------------------------- | -------------------------------------- | -------------------------------------- | ---------------------------------------- | ---------------- |
| Bare                    | DeepSeek-V4-Flash                                   | **858/912 completed; 858 scored**§ |             | **47.90**§ | **40.33**§ | **100/100; 2.00/42.62/96.93/—**§§ | **5/97; 20.00/76.23/98.64/—**§§ | **100/100; 12.00/63.83/94.48/—**§§ | **205/297; 7.32/59.05/96.87/—/—**§§ | **—**            |
|                         | Qwen3-Coder-480B                                    | **868/912 completed; 903 scored**§ |             | **29.34**§ | **24.54**§ | **100/100; 0.00/35.92/95.44/—** | **97/97; 0.00/4.52/98.34/—** | **100/100; 0.00/31.42/94.56/—** | **297/321 cell-based outputs; 0.00/24.15/96.09/—/—** | **24/24§§**       |
| Trace2Skill             | DeepSeek-V4-Flash                                   | **885/912 valid; 730 scored snapshot**† |             | **48.01**† | **30.96**† | **100/100; 0.00/48.21/99.23/97.70**‡ | **95/97; 18.56/79.23/98.51/93.00**‡ | **100/100; 19.00/80.29/99.78/99.19**‡ | **319/321; 295/297 cell-based outputs; 12.46/69.07/99.19/96.67/99.36**‡ | **24/24‡**          |
|                         | Qwen3-Coder-480B                                    | **800/912 valid; 912 evaluated (missing counted failed)**† |             | **34.58**† | **16.12**†  | **88/100 current (86 scored snapshot); 0.00/37.19/99.09/83.68**‡ | **94/97; 1.03/32.55/93.52/79.62**‡ | **97/100 current (96 scored snapshot); 0.00/33.76/99.68/93.13**‡ | **302/321 current; V2 retry stopped; 299 evaluated snapshot; 0.34/34.42/97.40/85.54/97.84**‡ | **23/24‡**          |
| SpreadsheetAgent        | DeepSeek-V4-Flash                                   | **42/912 terminal; 26 scored**† |           | **52.56**† | **46.15**† | **19/100 scored; 0.00/56.99/99.90/—**‡ | **— (0 scored)**‡ | **— (0 scored)**‡ | **19/297 scored; 0.00/56.99/99.90/—/—**‡ | **— (0/24 generated)**‡ |
|                         | Qwen3-Coder-480B                                    | **68/912 terminal; 57 scored**† |           | **38.01**† | **31.58**† | **38/100 scored; 0.00/44.51/99.26/—**‡ | **— (0 scored)**‡ | **— (0 scored)**‡ | **38/297 scored; 0.00/44.51/99.26/—/—**‡ | **— (0/24 generated)**‡ |
| ExternalHarness / Codex + `spreadsheet-core` | DeepSeek-V4-Flash | **858/912 recorded; 841 scored**† | | **55.01**† | **47.09**† | **97/100; 2.06/54.24/99.04/—** | **97/97; 16.49/73.52/98.47/—** | **100/100; 17.00/79.75/99.49/—** | **318/321; 294/297 non-Visualization scored; 11.90/69.28/99.00/—/—** | **24/24，待官方视觉评分** |
| ExternalHarness / Claude Code + `spreadsheet-core` | DeepSeek-V4-Flash | **760/912 recorded; 751 scored**† | | **54.51**† | **46.60**† | **99/100; 2.02/52.07/99.21/—** | **97/97; 17.53/73.87/98.77/—** | **100/100; 18.00/78.77/99.37/—** | **319/321; 296/297 non-Visualization scored; 12.50/68.23/99.12/—/—** | **23/24，待官方视觉评分** |
| ExternalHarness / DeepSeekHarness + `spreadsheet-core` | DeepSeek-V4-Flash | **796/912 recorded; 781 scored**† | | **54.03**† | **45.58**† | **99/100; 1.01/47.55/99.42/—** | **97/97; 19.59/73.54/98.78/—** | **99/100; 18.18/78.80/99.54/—** | **318/321; 295/297 non-Visualization scored; 12.88/66.58/99.25/—/—** | **23/24，待官方视觉评分** |
| ExternalHarness / Codex + `spreadsheet-core` | Qwen3-Coder-480B | **737/912 recorded; 673 scored**† | | **30.01**† | **25.11**† | **94/100; 0.00/41.64/99.43/—** | **97/97; 2.06/29.95/97.34/—** | **94/100; 0.00/36.18/99.51/—** | **309/321; 285/297 non-Visualization scored; 0.70/35.86/98.75/—/—** | **24/24，待官方视觉评分** |
| ExternalHarness / Claude Code + `spreadsheet-core` | Qwen3-Coder-480B | **912/912 recorded; 901 scored**† | | **32.30**† | **27.08**† | **100/100; 0.00/45.98/99.17/—** | **97/97; 2.06/40.67/97.77/—** | **100/100; 1.00/46.69/98.81/—** | **321/321; 297/297 non-Visualization scored; 1.01/44.49/98.59/—/—** | **24/24，待官方视觉评分** |
| ExternalHarness / DeepSeekHarness + `spreadsheet-core` | Qwen3-Coder-480B | **912/912 recorded; 876 scored**† | | **32.31**† | **27.05**† | **76/100; 0.00/45.02/99.15/—** | **76/97; 3.95/34.97/96.09/—** | **83/100; 1.20/43.11/99.48/—** | **259/321; 235/297 non-Visualization scored; 1.70/41.10/98.28/—/—** | **24/24，待官方视觉评分** |
| **Spreadsheet-RL (Tools+NativeHarness；无 RL)** | **DeepSeek-V4-Flash** | **912/912 processed; 897 scored**† | | **48.53**† | **40.69**† | **100/100; 0.00/39.09/98.68/—** | **97/97; 15.46/70.87/98.32/—** | **100/100; 3.00/37.20/99.99/—** | **297/297 non-Visualization; 6.06/48.83/99.00/—/—** | **— (24 excluded)** |
|                         | **Qwen3-Coder-480B** | **912/912 processed; 908 scored**† | | **34.65**† | **29.07**† | **100/100; 0.00/35.68/99.03/—** | **97/97; 2.06/27.89/96.65/—** | **100/100; 0.00/36.42/94.52/—** | **297/297 non-Visualization; 0.67/33.39/96.73/—/—** | **— (24 excluded)** |
| **SheetHarness (Ours，Financial)** | **DeepSeek-V4-Flash** | **200/200 recorded; 190 scored** | | **32.46** | **26.32** | **99/100; 8.08/44.51/97.46/—** | **97/97; 19.59/68.55/97.44/—** | **97/100; 22.68/68.52/94.82/—** | **293/297 non-Visualization completed** | **—** |
|                         | **Qwen3-Coder-480B** | **200/200 recorded; 192 scored** | | **29.69** | **26.04** | **99/100; 5.00/38.93/97.06/—** | **97/97; 8.25/23.20/96.31/—** | **97/100; 10.00/46.86/95.78/—** | **293/297; 7.74/36.35/96.39/—/—** | **23/24，待官方视觉评分** |
| **SheetHarness (Ours，Basic)** | **DeepSeek-V4-Flash** | **200/200 recorded; 197 scored** | | **26.73** | **21.83** | **97/100 scored; 7.22/44.03/99.51/—** | **91/97 scored; 23.08/71.81/97.78/—** | **99/100 scored; 10.10/59.29/99.90/—** | **321/321 recorded; 287/297 non-Visualization scored; 13.24/58.10/99.10/—/—** | **13/24 generated，待官方视觉评分** |
|                         | **Qwen3-Coder-480B** | **200/200 recorded; 198 scored** | | **26.60** | **22.73** | **100/100 scored; 5.00/39.99/99.12/—** | **97/97 scored; 8.25/20.11/96.23/—** | **97/100 scored; 0.00/41.13/99.73/—** | **321/321 recorded; 294/297 non-Visualization scored; 4.42/33.81/98.37/—/—** | **22/24 generated，待官方视觉评分** |

表中 V1 Coverage 为 `有效输出/总任务`，并在可追溯时附上实际进入 evaluator 的 scored 数；这里的 scored 以 `status=completed` 且存在 Soft/Hard 分数为准。Bare DeepSeek V1 full 的 `858/912 completed; 858 scored`、Soft=`47.90`、Hard=`40.33` 是 matched 200-task 与剩余 712-task 的官方 V1 score 合并结果；原始 `outcome_kind` 字段有 36 条 `not_scored` 记录被错误标成 `scored`，因 Soft/Hard 为空，已排除。Bare Qwen 的 `868/912 completed; 903 scored`、Soft=`29.34`、Hard=`24.54` 来自 2026-09-15 全量阶段性结果。Trace2Skill V1 Qwen 的最新已评分快照为 Soft=`34.58`、Hard=`16.12`（2026-09-17）；之后继续补跑时因 DashScope `429 QUOTA_EXHAUSTED` 中断，当前落盘为 `800/912` valid、`2596/2729` cases，不能把未重新评测的当前落盘状态与该评分快照混用。Trace2Skill V1 DeepSeek 的 48.01/30.96 仍是 730-task 旧快照，不能视为当前 885 个有效输出的重评结果。SheetHarness V1 两行来自同一个 200-task representative subset，DeepSeek 为 `190 scored + 10 not_scored`，Qwen 为 `192 scored + 8 not_scored`；Soft/Hard 仅按有评分任务计算，结果目录分别为 `benchmarks/results/ours-deepseek-v1-run2-20260916` 和 `benchmarks/results/ours-qwen-v1-run2-20260916`。V2 子集列先记录有效输出情况，再按 `Exact / Modification / Regression / Macro cell` 顺序记录官方 V2 cell-based comparator 指标；若当前有效输出多于评分快照，会同时标出 `current` 和 `scored snapshot`。V2 Overall 列再按 `Exact / Modification / Regression / Macro cell / Micro cell` 顺序记录合计结果，数值均为百分比。Bare Qwen V2 的 R3 目录已覆盖全部 321 个任务：297/297 个 cell-based task 均为 `completed/scored`，24/24 个 Visualization task 均有有效产物；其 `0.00` 表示 Exact 为 0，不表示没有输出。Bare DeepSeek V2 当前快照为 Debugging 100、Financial 100、Template 5，共 205 个已有官方 task summary；这些是运行中的阶段性分数，不是最终 321-task 结果。SheetHarness DeepSeek 的三个 V2 子集当前共完成 `293/297` 个非 Visualization 样例，Macro 尚未提供，因此记为 `—`；Visualization 也暂留空。SheetHarness Qwen V2 已遍历全部 321 个任务，其中 Debugging、Financial、Template 分别有 `99/100`、`97/100`、`97/97` 个官方评分，共 `293/297`；Exact 按各子集总任务数（缺失评分计失败）计算，Modification/Regression 按有效官方评分计算。其 23 个 Visualization 产物仅代表生成完成，尚未获得官方 Windows/VLM 分数；另有 1 个 Visualization 任务因重算完整性错误未生成有效产物。当前结果文件未提供 cell-level correct/total 计数，故 SheetHarness Qwen 的 Macro/Micro 记为 `—`。`†` 表示该运行仍有未完成任务或 V1 评分只覆盖阶段性结果；`‡` 表示 V2 数值来自尚未覆盖当前全部有效输出的阶段性快照，不能当作最终 321-task 结果；`§` 表示 V1 结果为全量阶段性合并；`§§` 表示 Bare V2 已完成产物生成但 Visualization 仍待官方 Windows/VLM 评分。Visualization 的 `24/24` 或 `23/24` 只表示 XLSX 产物数量，尚未完成 Windows Excel/WPS + `glm-4.6v` 官方视觉评分，不能按 0 分计入。

上面六行的 V1 列是 SpreadsheetBench V1 全量外部 harness 当前快照（2026-09-18 10:29 CST）；V1 Soft/Hard 只对已经完成官方 sibling replay 且有分数的 task 求均值，未评分 task 不补零，带 `†` 的 V1 数值仍不是最终 912-task 成绩。六组均使用 `spreadsheet-core`、`max-turns=50`、`temperature=0`、`top_p=1`、thinking on、reasoning effort `medium` 和内网 LiteLLM。

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
