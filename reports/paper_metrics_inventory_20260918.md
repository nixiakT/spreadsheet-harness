# 可用于论文的结果指标清单（2026-09-18）

这份清单只汇总仓库中已经存在、可追溯到原始结果的数值；空白不补零。所有百分比均为百分点口径，除非另有说明。

## 先说结论：哪些表现在能填

| 目标 | 当前状态 | 建议写法 |
|---|---|---|
| 截图中的 Table 2（full adaptive-scope 七行） | **不能填最终版** | `method-adaptive-scope-v4.../workspace/state.json` 仍为 active，尚无 scored outcome。 |
| 截图中的 Table 3（同一 full adaptive-scope run 的四格交互） | **不能填最终版** | formal paper36 held-out 四格没有结果；`hd_interaction.csv` 仍是预注册占位。 |
| 24-family accelerated held-out pilot 的 Table 2/3 | **可以填，但必须标 pilot** | `paper24-heldout-pilot.../TABLES.md`、`report.json` 已 complete；不是 269/1,565 全量正式结果。 |
| 截图中的 Table 4（general-plugin ablation） | **不能填** | `method-operator-ablation-v3.../report.json` 显示三臂均 active、0 accepted round、无 outcome。 |

因此，论文正文目前最稳妥的组合是：把 24-family pilot 放在“pilot/diagnostic”小节或补充材料；把 V2 Basic/Financial、插件路由、验证 gate 和成本指标放入主结果/分析；不要把 pilot 改名成 full formal held-out，也不要把 adaptive ablation 的 TBD 改成猜测值。

## A. 24-family held-out pilot：可直接生成 Table 2/3 的数值

来源：`benchmarks/results/paper24-heldout-pilot-qwen36plus-20260917/report.json`。每个 suite 12 个独立 workbook family；报告 `complete=true`。这是 accelerated preregistered pilot，不是完整 Fin-269/Fin-1.5K 评测。

### A1. Task Success（截图 Table 2 最接近的可用版本）

| Variant | Fin-269 TS | Fin-1.5K TS |
|---|---:|---:|
| Financial initialization | 8.3 [0.0, 25.0] | 16.7 [0.0, 41.7] |
| General-only | 8.3 [0.0, 25.0] | 16.7 [0.0, 41.7] |
| Domain-only | 8.3 [0.0, 25.0] | 16.7 [0.0, 41.7] |
| Alternating (pilot) | 8.3 [0.0, 25.0] | 25.0 [0.0, 50.0] |

括号是 family-bootstrap 95% CI。General-only、Domain-only、Alternating 的 Exact paired test（每 suite、12 对）分别为：Fin-269 全部 0–0–12（candidate win / initial win / tie）；Fin-1.5K 的 Alternating 为 1–0–11，其余为 0–0–12；Holm 校正后均为 1。

### A2. 补充指标：Modification / Regression

| Variant | Fin-269 Modification | Fin-269 Regression | Fin-1.5K Modification | Fin-1.5K Regression |
|---|---:|---:|---:|---:|
| Financial initialization | 33.49 [19.70, 49.38] | 97.27 [96.02, 98.37] | 48.23 [27.07, 69.74] | 97.72 [95.65, 99.36] |
| General-only | 30.95 [16.41, 48.07] | 97.40 [96.16, 98.50] | 49.69 [26.05, 72.41] | 97.94 [95.58, 99.68] |
| Domain-only | 28.85 [15.54, 45.26] | 97.40 [96.13, 98.55] | 48.52 [29.61, 68.31] | 98.20 [96.54, 99.50] |
| Alternating (pilot) | 29.27 [16.53, 45.24] | 97.33 [96.08, 98.43] | 38.67 [16.56, 62.24] | 98.68 [96.97, 99.79] |

可写出的稳妥观察是：pilot 中 Regression 一直约 97–99%，但 Exact/Modification 仍低，说明“保持未涉及单元格不变”不是主要瓶颈；真正瓶颈是目标修改正确性和端到端 exact pass。Alternating 在 Fin-1.5K 多 1 个 exact family，但 modification mean 比 initialization 低 9.56 pp，不能把它表述成全面提升。

### A3. Endpoint component interaction（pilot Table 3）

公式为 `I = S11 - S10 - S01 + S00`，单位为 percentage points。

| Suite | C00 initial pair | C10 evolved H / initial D | C01 initial H / evolved D | C11 evolved pair | Interaction I |
|---|---:|---:|---:|---:|---:|
| Fin-269 | 8.3 | 8.3 | 0.0 | 8.3 | **+8.3** [0.0, 25.0] |
| Fin-1.5K | 16.7 | 16.7 | 25.0 | 25.0 | **+0.0** [-25.0, 25.0] |

这组结果支持“存在 suite-dependent 的非加性迹象”，但 CI 很宽，不能据此声称有稳定 joint-operator benefit。截图中的 full adaptive-scope 交互仍然缺正式 held-out 数据。

### A4. 可作为补充的 weighted artifact score

按当前协议的 `W=(Accuracy+0.25*Modification+0.10*Regression)/1.35` 由 report 均值推导：

| Variant | Fin-269 W | Fin-1.5K W | 相对 initialization |
|---|---:|---:|---:|
| Initialization | 19.58 | 28.52 | — |
| General-only | 19.12 | 28.80 | -0.46 / +0.29 |
| Domain-only | 18.73 | 28.60 | -0.85 / +0.09 |
| H-only composition | 19.14 | 27.62 | -0.44 / -0.90 |
| D-only composition | 11.77 | 34.99 | -7.81 / +6.47 |
| Alternating | 18.80 | 32.99 | -0.78 / +4.48 |

这个分数适合放补充材料，因为它把三个不同性质的指标压成一个数；正文仍应同时报告 TS、Modification、Regression。

## B. V2 Basic → Financial：目前最有说服力的静态插件证据

来源：`paper_fig_data/static_plugin_gains.csv`，使用同配置 DeepSeek-V4-Flash V2 run。Financial route 有已知 early-stop / provider-flow 缺陷，因此这组数可作为可追溯结果和机制诊断，不能写成无条件的最终 SOTA 结论。

### B1. DeepSeek V2 的增益按 category 分解

| Category | Basic Exact | Financial Exact | Δ Exact | Basic Modification | Financial Modification | Δ Modification |
|---|---:|---:|---:|---:|---:|---:|
| Overall | 11.86 | 16.72 | **+4.86** | 54.53 | 60.42 | **+5.89** |
| Template | 24.74 | 19.59 | -5.15 | 69.20 | 68.55 | -0.65 |
| Financial Modeling | 4.00 | 22.68 | **+18.68** | 51.17 | 68.52 | **+17.35** |
| Debugging | 7.14 | 8.08 | +0.94 | 43.43 | 44.51 | +1.08 |

适合正文的叙事是“domain plugin 的收益是 category-local 的”：Financial Modeling Exact 从 4.00 提升到 22.68（约 5.67×，+18.68 pp），而 Template Exact 下降 5.15 pp；整体 +4.86 pp 主要由 Financial Modeling 贡献，而不是所有类别均匀提升。

### B2. Exact 的近似 binomial CI（补充材料可用）

由已报告的 score 和 scored denominator 还原成功数，给出未配对 Wilson 95% CI；这不是 paired CI。

| Run | Exact successes / scored | Exact | Wilson 95% CI |
|---|---:|---:|---:|
| DeepSeek Basic overall | 35 / 295 | 11.86 | [8.66, 16.05] |
| DeepSeek Financial overall | 49 / 293 | 16.72 | [12.89, 21.42] |
| Qwen Basic overall | 13 / 294 | 4.42 | [2.60, 7.42] |

### B3. 成本/可靠性 trade-off

DeepSeek Basic → Financial 的平均模型调用从 18.02 增到 25.00（+38.7%），平均总 token 从 297,784.65 增到 394,189.29（+32.4%，+96,404.64）；Exact +4.86 pp、Modification +5.89 pp；invalid output rate 5.39%→7.41%，provider-failure rate 0→2.02%。描述性地，整体 Exact 增益约为每增加一次模型调用 0.70 pp、每增加 100k tokens 5.04 pp；该比值只用于成本敏感性分析，不是因果效率估计。

## C. Backbone / harness robustness

### C1. Qwen Basic 与 DeepSeek Basic 的 V2 差异

在同一 V2 category 表上，Qwen Basic 相对 DeepSeek Basic 的差值为：

| Category | Δ Exact (Qwen − DeepSeek) | Δ Modification (Qwen − DeepSeek) |
|---|---:|---:|
| Overall | -7.44 | -20.72 |
| Template | -16.49 | -49.09 |
| Financial Modeling | -4.00 | -10.04 |
| Debugging | -2.14 | -3.44 |

这支持“方法效果与 backbone 强交互”的分析；不要把某一个 backbone 的绝对分数直接泛化成方法本身的普遍能力。

### C2. 外部 harness 的稳定性区间

`benchmarks/reports/spreadsheetbench-v2-harness-report-20260915.md` 给出的 DeepSeek + spreadsheet-core 三种 harness：Overall Exact **11.90–12.88**、Regression **99.00–99.25**、平均请求 **21.24–26.17/题**。Qwen + spreadsheet-core 三种 harness：Overall Exact **0.70–1.70**、Regression **98.28–98.75**、平均请求 **11.70–40.55/题**。

可写成：Regression 在所有组合都很高（约 98.3–99.6%），但 Exact 对 backbone/harness 极其敏感；“完成任务/不破坏原表”与“精确修对目标单元格”是分离的能力维度。

## D. Runtime plugin routing：可形成机制图或补充表

来源：`paper_fig_data/plugin_usage_heatmap.csv`、`paper_fig_data/task_plugin_composition.csv`。下面只把 invocation/activation 作为可审计 runtime 信号，不把它解释为模型内部因果贡献。

### D1. Financial plugin 的 task-aware 路由

在 DeepSeek Financial composition 中，`skill-spreadsheet-financial-model` 的 activation rate：

| Category | D plugin activation | FINANCIAL 平均 active plugin 数 |
|---|---:|---:|
| Template | 81.44% | 6.742 |
| Financial Modeling | **100.00%** | 6.640 |
| Debugging | 0% | 5.890 |
| Visualization | 0% | 5.917 |

BASIC composition 没有 D plugin；因此可以严谨地说“插件路由具有 category-aware 分工”，不能说“该插件导致了全部 Exact 增益”。

### D2. Loaded 与真正 invoked 的区别

DeepSeek Financial：4,173 个 loaded task-plugin slots 中 2,049 个有 invocation signal（49.10%）；DeepSeek Basic：3,852 个 loaded slots 中 1,797 个 invoked（46.65%）。这可以支撑“resolved composition 不是等于每题都执行，runtime routing 是 task-aware”的方法叙事。

### D3. 典型调用模式

DeepSeek Financial 非视觉 293 个有 score 的 task 上，条件 exact rate（仅描述性、未控制 category/难度）为：

- 调用 `skill-spreadsheet-financial-model`：22.73%；未调用：7.69%。
- 调用 `skill-spreadsheet-formula`：17.27%；未调用：6.67%。
- 调用 `skill-spreadsheet-analysis`：21.43%；未调用：15.94%。

这些是选择偏差明显的 observational split，适合做 routing figure 或 error analysis，不应写成插件的因果 ablation。

## E. Co-evolution search / gate 行为

来源：`paper_fig_data/evolution_trajectory.csv` 及三个 `paper36-search-*` decision roots。这里是 7-family validation gate，不是 held-out test。

- 共 12 个 candidate proposals：4 个 promote、8 个 reject；promotion rate **33.3%**，reject rate **66.7%**。
- H proposals：4 个、接受 2 个（50%）；D proposals：8 个、接受 2 个（25%）。
- 8 个 reject 中，7 个 replay gate failure、1 个 regression gate failure；没有把失败候选悄悄并入 endpoint。
- General-only endpoint 的 weighted quality 相对 baseline **+2.54 pp**；Domain-only **+1.72 pp**；Alternating endpoint **+17.31 pp**（52.8351→70.1464）。
- Alternating 中 first H proposal 被接受，随后 5 个 D proposals 中仅 1 个被接受；这支持“严格 replay/regression gate 过滤不稳定跨组更新”的过程性叙事。

应明确写成 validation/search dynamics；因为 formal held-out 仍未打开/没有 score，这些数不能代替 Table 2/3 的 test result。

## F. 推荐的论文叙事组合

### 主文可以放

1. V2 Basic→Financial 的 category 分解：Overall +4.86 Exact、Financial Modeling +18.68 Exact、Template -5.15 Exact。
2. Regression 高而 Exact 低的能力分解：例如 DeepSeek Basic Overall 11.86 Exact vs 54.53 Modification；Financial 16.72 vs 60.42。
3. D plugin 的 category-aware routing：Financial Modeling 100%，Template 81.44%，Debugging/Visualization 0%。
4. 成本 trade-off：Financial route +38.7% calls、+32.4% tokens，换取 +4.86 Exact pp。

### 补充材料可以放

1. 24-family pilot 的完整 Table 2/3、bootstrap CI 和 paired exact test。
2. 12-proposal evolution trajectory、promotion/rejection reason、endpoint weighted quality。
3. loaded-vs-invoked、plugin pattern count、conditional success split。
4. Qwen/DeepSeek 与 external harness robustness 表。

### 目前不要写成正式结论

- full adaptive-scope 七变体的最终比较；对应 run 仍 active。
- Table 4 general-plugin ablation；三臂均无最终 outcome。
- formal paper36 12-family held-out interaction；`hd_interaction.csv` 仍 missing。
- 把 24-family pilot 当成 Fin-269/Fin-1.5K 全量结果。
- 把已知 early-stop/provider-flow 缺陷的旧 Financial run 称为干净 final SOTA。

## 原始证据索引

- [24-family pilot report](../benchmarks/results/paper24-heldout-pilot-qwen36plus-20260917/report.json)
- [24-family pilot tables](../benchmarks/results/paper24-heldout-pilot-qwen36plus-20260917/TABLES.md)
- [static plugin gains](../paper_fig_data/static_plugin_gains.csv)
- [accuracy/cost trade-off](../paper_fig_data/accuracy_cost_tradeoff.csv)
- [plugin usage heatmap](../paper_fig_data/plugin_usage_heatmap.csv)
- [task × plugin composition](../paper_fig_data/task_plugin_composition.csv)
- [evolution trajectory](../paper_fig_data/evolution_trajectory.csv)
- [V2 harness report](../benchmarks/reports/spreadsheetbench-v2-harness-report-20260915.md)
- [formal held-out audit](../paper_fig_data/README.md)
- [method-operator ablation status](../benchmarks/results/method-operator-ablation-v3-qwen36plus-glm52-20260918/report.json)
