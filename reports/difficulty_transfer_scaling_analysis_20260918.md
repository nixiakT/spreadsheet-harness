# 难度、跨模型迁移与 token scaling 分析

分析日期：2026-09-18。主要依据 DeepSeek-V4-Flash V2 的 paired Basic/Financial 结果、SpreadsheetBench-v2 answer-position 元数据，以及一个共享 composition SHA 的 Qwen3.6-35B / GPT-5.5 matched-task smoke。

## 1. 最强的新结果：domain harness 的收益集中在跨 sheet 题

### 1.1 难度定义

当前最稳妥的可审计定义不是“题目文字看起来难”，而是目标答案区域涉及的 workbook sheet 数：

- 1 sheet：`answer_position` 只涉及一个 sheet；
- 2–3 sheets：涉及两个到三个 sheet；
- 4+ sheets：涉及至少四个 sheet。

这是目标修改范围的结构难度，不等于完整 workbook 的 sheet 数。另一个可用的增强难度特征是 workbook 中公式数量、非空 cell 数和跨 sheet 引用数量。

### 1.2 DeepSeek V2 paired Exact

| Target-sheet complexity | Paired tasks | Basic Exact | Financial Exact | Δ Financial−Basic | Financial paired wins/losses |
|---|---:|---:|---:|---:|---:|
| 1 sheet | 81 | 28.40 | 18.52 | **−9.88 pp** | 0 / 8 |
| 2–3 sheets | 26 | 3.85 | 15.38 | **+11.54 pp** | 3 / 0 |
| 4+ sheets | 185 | 5.95 | 16.22 | **+10.27 pp** | 19 / 0 |

这不是简单的总体 category 差异：同一 paired task 上，Financial composition 在 211 个 multi-sheet 有效配对中没有 Exact loss，Basic 只在 single-sheet 任务上占优势。

### 1.3 结构难度的其他分层

| Feature | Low bucket | Basic | Financial | Δ | High bucket | Basic | Financial | Δ |
|---|---|---:|---:|---:|---|---:|---:|---:|
| Non-empty cells | ≤500 | 24.74 | 19.59 | −5.15 pp | >10,000 | 2.78 | 13.89 | **+11.11 pp** |
| Formula count | ≤20 | 23.86 | 17.05 | −6.82 pp | >500 | 5.79 | 15.79 | **+10.00 pp** |
| Cross-sheet formula rate | 0 | 25.00 | 18.75 | −6.25 pp | >15% | 5.77 | 19.23 | **+13.46 pp** |

这里的 high/low 桶是描述性分层，不是预注册阈值。最值得写的机制解释是：Financial plugin 对跨表依赖和大 workbook 的帮助明显，而对简单、单表、低结构复杂度任务反而可能增加 routing/interaction overhead。

### 1.4 推荐论文表述

> The domain-specific harness does not improve all tasks uniformly. Its gain is concentrated on structurally difficult workbooks: on paired tasks spanning two or more target sheets, Financial improves Exact accuracy by approximately 10–12 percentage points over Basic, whereas it is lower on single-sheet tasks. The same pattern appears when difficulty is measured by workbook size, formula count, and cross-sheet formula density.

注意：这是 paired observational stratification，不是独立的 difficulty intervention；不同复杂度桶的 task composition 仍可能不同。

## 2. Token 消耗：正确答案并不简单地随着 token 增加而增加

### 2.1 DeepSeek V2 的逐题 token 结果

| Composition | Scored Exact | Mean total tokens | Median total tokens | Mean turns | Exact-correct token median |
|---|---:|---:|---:|---:|---:|
| Basic | 295 | 328,193 | 128,405 | 19.37 | 109,927 |
| Financial | 293 | 450,805 | 361,684 | 26.63 | 239,872 |

Financial 平均多消耗约 122.6k tokens、7.26 turns，但 overall Exact 只增加 4.86 pp。因此“更长”本身不是充分条件。

### 2.2 Token quartile curve

| Composition | Token quartile | Median tokens | Exact |
|---|---|---:|---:|
| Basic | Q1 | 约 12k | 2.90% |
| Basic | Q2 | 约 67k | 17.39% |
| Basic | Q3 | 约 272k | 10.14% |
| Basic | Q4 | 约 840k | 5.63% |
| Financial | Q1 | 约 75k | 12.50% |
| Financial | Q2 | 约 190k | 12.31% |
| Financial | Q3 | 约 485k | 21.54% |
| Financial | Q4 | 约 840k | 0.00% |

可写为：

> Exact success follows an inverted-U-like, non-monotonic relationship with interaction budget. Moderate interaction appears beneficial, while the highest-token quartile does not improve and can collapse to zero Exact in the Financial run, consistent with over-searching, late destructive edits, or unresolved verification loops.

这只能叫 token–accuracy association，不能叫 scaling law；token 数是模型行为和 task difficulty 的共同结果，存在明显 endogeneity。

### 2.3 “答对的 token 消耗”

在已有 scored tasks 中，correct task 的 token 统计为：

| Composition | Correct tasks with token log | Correct mean tokens | Correct median tokens | Correct mean turns |
|---|---:|---:|---:|---:|
| Basic | 25 | 227,271 | 109,927 | 16.64 |
| Financial | 30 | 336,664 | 239,872 | 25.37 |

正确任务平均比全部 scored task 更省 token，但 Financial 的正确题仍比 Basic 的正确题更贵。这支持一个更细的叙事：Financial route 提高了复杂任务的上限，但它的成功需要更长的 verification/execution trajectory。

### 2.4 推荐效率指标

现有 overall 增益可以写成描述性成本比：

- Financial 相对 Basic：+6.98 model calls/task、+96,405 tokens/task；
- Exact +4.86 pp；
- 约 0.70 Exact pp / additional model call；
- 约 5.04 Exact pp / additional 100k tokens。

这两个比值不要作为因果效率或 scaling exponent，只作为 cost-sensitivity summary。

## 3. 跨模型迁移：现有证据支持“composition 可迁移”，但还不足以支持成功率迁移

### 3.1 共享 composition smoke

当前发现一个最干净的 matched-task smoke：`Debugging/01_04`，Qwen3.6-35B 和 GPT-5.5 使用同一个 `spreadsheet-harness-basic` composition SHA（`67bae5f0...`）。

| Model | Exact | Modification | Regression | Total tokens | Turns |
|---|---:|---:|---:|---:|---:|
| Qwen3.6-35B | 0.0% | 49.69% | 100.0% | 98,315 | 7 |
| GPT-5.5 | 0.0% | 49.69% | 100.0% | 27,885 | 2 |

可观察到：

- 三个 evaluator 指标完全一致；
- GPT-5.5 在该题上使用约 71.6% 更少 token、5 turns 更少；
- 但两者都没有达到 Exact pass。

这说明 harness 的结构化执行/验证行为可以跨 backbone 复用；不能据此声称“Qwen 上进化出的 harness 已成功迁移到 GPT”，因为当前只有一个 matched task，而且 Exact 都是 0。

### 3.2 迁移结论的正确分级

| Claim | 当前证据 | 是否可写 |
|---|---|---|
| Same composition can run on both backbones | 同 composition SHA、同 task、两端均有 score | **可以** |
| Modification/Regression behavior transfers | smoke 中两个模型数值完全相同 | **可以写成 preliminary** |
| Exact success rate transfers | 两端均 0%，n=1 | 不可以 |
| Evolved Qwen harness improves GPT | 没有 GPT matched baseline/evolved paired matrix | 不可以 |
| GPT needs fewer tokens under same harness | smoke 中 GPT 27.9k vs Qwen 98.3k | 可以写成 exploratory |

### 3.3 需要补跑的最小迁移实验

如果要把迁移写成正式论文结果，最小设计是：

| Arm | Backbone | Composition | Tasks |
|---|---|---|---:|
| GPT baseline | GPT-5.5 | Basic baseline | 同一 30–50 task subset |
| GPT transferred | GPT-5.5 | Qwen-evolved/frozen harness | 同一 subset |
| Qwen baseline | Qwen3.6-35B | Basic baseline | 同一 subset |
| Qwen evolved | Qwen3.6-35B | evolved harness | 同一 subset |

主要指标：Exact、Modification、Regression、success-per-100k-token，以及 paired task-level Δ。

## 4. Scaling 曲线：目前适合画“interaction scaling”，不适合声称模型 scaling law

### 4.1 可以直接画的曲线

1. `fig8_token_exact_curve`：token quartile → Exact。它展示的是 interaction-budget association。
2. Basic → Financial 的 category gain curve：按 single-sheet / multi-sheet 分层。
3. 每题 tokens vs Modification/Regression scatter：可以区分“多花 token 是在修目标，还是在保护未修改单元格”。
4. 模型调用数 → Exact / Modification 的 binned curve：比直接拟合 token 更容易解释。

### 4.2 目前不能严谨画的曲线

- 模型参数量 → Exact：现有 Qwen3.6-35B、Qwen3-Coder-480B、DeepSeek-V4-Flash、GPT-5.5 的协议、temperature、thinking、task coverage 和 evaluator snapshot 不完全一致。
- token budget → Exact 的因果 scaling：没有固定 backbone/task、随机分配 token budget 的实验。
- evolved rounds → held-out Exact：正式 full held-out evolution matrix 仍缺失。

### 4.3 可写的 scaling 叙事

> More interaction is not uniformly better. The observed curves are non-monotonic: moderate token budgets improve Exact, while the longest trajectories often correspond to the hardest tasks and may accumulate late-stage errors. The harness benefit is therefore better characterized as difficulty-adaptive compute allocation than as a monotonic scaling law.

## 5. 推荐新增图

已生成：

- [Fig 7: target-sheet harness gain](../paper_figures/20260918/fig7_target_sheet_harness_gain.pdf)
- [Fig 8: token–Exact curve](../paper_figures/20260918/fig8_token_exact_curve.pdf)
- [Fig 9: cross-model transfer smoke](../paper_figures/20260918/fig9_cross_model_transfer_smoke.pdf)

PNG/SVG 版本也在同一目录。复现脚本为 [generate_paper_figures.py](../paper_fig_data/generate_paper_figures.py)。

## 6. 最终建议

主文最值得放的是 Fig 7：它直接回答“越难题 harness 越好吗”，而且结果最清楚：single-sheet 不占优，multi-sheet 提升约 10–12 pp。

补充材料放 Fig 8 和 Fig 9：

- Fig 8 支持“token scaling 非单调”；
- Fig 9 支持“同一 harness composition 可跨模型运行”，但明确标注 `n=1 smoke` 和 `Exact=0`。

不要把 Fig 9 的结果写成 GPT 正式迁移成功，也不要把 Fig 8 拟合成 parameter scaling law。
