# V1 静态方法退化审计与修复（2026-09-18）

后续更新：为满足“不影响 V2”的要求，下述修复现已改为 V1 显式 opt-in 的 `--v1-execution-mode repaired`；另提供 `direct` 候选。默认 `legacy` 保留旧流程，V2 不启用这些变更。在线 canary 已启动，参见 `analyse/v1_isolated_fix_20260918.md`。下文“未启动模型请求”描述的是首轮诊断结束时的状态。

结论：DeepSeek 的 Basic/Financial 退化有真实的共享 harness 实现缺陷，不能仅归因于模型能力或金融插件负迁移。Qwen 在共同任务上并不存在同等幅度的退化。代码修复已完成；尚未发起新模型请求，不能声称新 benchmark 分数已经提高。

## 同任务重算

原截图将接近 912 题的 Bare 阶段性结果与 200 题插件子集放在一起，且分母按各自有效评分数变化。以下只保留三臂都有有效 Soft/Hard 的 task ID：这是诊断性交集，不能替代固定全任务分母的正式结果，也未控制不同运行日期/源码/模型服务波动。

| 模型 / 共同已评分任务 | 方法 | Soft % | Hard % | Case 1 通过数 |
|---|---|---:|---:|---:|
| DeepSeek / 184 | Bare | 46.92 | 38.59 | 97 |
| | Basic | 28.62 | 23.37 | 60 |
| | Financial | 32.97 | 26.63 | 68 |
| Qwen / 190 | Bare | 28.42 | 23.68 | 57 |
| | Basic | 27.72 | 23.68 | 56 |
| | Financial | 30.00 | 26.32 | 60 |

DeepSeek 的 Case 1 已相差 37/29 个通过任务，不能把退化主要解释为 case 2/3 重放问题。Qwen Financial 反而比 Bare 更高；现有单次运行不能证明差异显著。

## 已复现的实现问题

### 1. YAML 展示转义污染可执行公式

`_yaml_evidence()` 对 `safe_dump()` 的整个输出替换尖括号。YAML 普通/单引号字符串不会解析 `\u003e`，因此 `=IF(B2>0,1,0)` 被写成 `=IF(B2\u003e0,1,0)`。另外，action applier 把任何含 `<`/`>` 的字符串误判成 placeholder，正常比较公式会被跳过。V1 replay 使用原始 planner 文本，旧实现还可能导致 case 1 与 sibling 执行的内容不同。

本次修复：含尖括号的字符串使用 YAML 双引号标量后再做展示转义，使 `safe_load` 严格还原原值；保留真实反斜杠文本，不做全局 Unicode 解码。收窄 placeholder 检查，允许比较运算符和公式字符串中的 HTML delimiter，仍拒绝 `<source>`、`TBD`、`{range}` 等占位符。加入原文→规范化→解析→实际写入→sibling replay 一致性测试。

### 2. V1 的无类别路由把“持久化成功”误当“任务完成”

V1 runner 给 `run_arm()` 传 `task_category=None`。通用 planner 路径原先允许：三个以上 verified writes → executor_turns=0。这些 verified 只证明单元格被写入，不能证明覆盖全部范围、操作类型正确或公式计算正确。当前 V2 Template/Financial 路径没有这个通用提前结束条件。

本次修复：通用 planner 的 warm-start 后必须进入既有 executor 阶段，不增加总预算，不改评分协议；保留独立 debugging-specific 路径。无类别与 Financial 类别均有测试，确保三个 verified writes 不能直接跳过 executor。

### 最新表格对应轨迹的观测

| 模型 / 方法 | 已评分 | verified 后没有 executor | 其中 Case 1 失败 | verified action 含转义痕迹 | 其中 Case 1 失败 |
|---|---:|---:|---:|---:|---:|
| DeepSeek Basic | 197 | 86 | 68 | 31 | 27 |
| DeepSeek Financial | 190 | 79 | 57 | 26 | 22 |
| Qwen Basic | 198 | 26 | 25 | 10 | 10 |
| Qwen Financial | 192 | 21 | 18 | 8 | 8 |

这些计数可重叠，不能相加，也不是预计可挽回的成功数。转义痕迹是定位线索，不代表每个失败都由它造成。强制执行器也可能带来额外费用或回归，必须用新运行验证。

### 3. Financial skill 在这些 V1 运行中没有进入选中技能

四组已评分 V1 的 `harness.skills.routed.selected` 中，`spreadsheet-financial-model` 均为 0。当前路由只在 `Financial_Model` 类别或满足条件的 `Template` 类别选中该领域技能，而 V1 无类别。因此不能将现有 Financial/Basic 差异解释成金融提示词的因果贡献。这里不代表所有 domain runtime 分支都未执行，也不代表应该对全部 V1 强制启用金融插件。

本次不改领域路由，避免与共享执行修复混成一个无法归因的变量。后续可单独验证基于用户指令的领域路由，不应读取 task ID、gold 或隐藏 answer_position。

## 为什么 V2 相对更好不矛盾

本地 V1 runner 生成 case 1 的解法后冻结重放到 case 2/3；V2 是针对各任务 workbook 运行并按不同 comparator 评分，两个版本的绝对百分比不应横向解释为相同难度。V2 显式类别触发了不同的执行路径和领域技能；当前通用的 V1 提前结束缺陷并不等量作用于 V2。V2 Exact 约 11–16% 只是相对基线有收益，不是绝对准确率很高。

## 验收与下一步

- 已添加 round-trip、真实 workbook 写入、placeholder 反例、无类别 executor 及 V1 sibling 一致性回归测试。
- 验证：`test_arms.py`、`test_spreadsheetbench_v1.py`、`test_spreadsheetbench_v2.py`、`test_plugins.py`、`test_financial_model_repairs.py` 共 **236 passed**（15.54 秒）；变更涉及文件的 Ruff 检查通过。
- 未修改原始结果、官方评分器、数据集、论文图表；未启动 API 请求或付费重跑。
- 先冻结修复后源码、相同 endpoint/model/seed/预算和固定 task 列表，三臂做小规模配对 canary；保留旧输出，记录全部未评分和基础设施错误。
- 核对 Case 1、Soft/Hard、完整任务分母、实际选中技能、调用数/token 成本以及各 case 的 replay 故障。若进入正式 200/912 题实验，再按预声明规则处理基础设施失败，不仅报告成功评分交集。
- 已看过的失败案例只用于回归；另选未检查案例确认泛化，必要时重复运行估计采样波动。不能保证仅靠这两处修复就追平 Bare。

## 复现与证据

运行 `.venv/bin/python analyse/audit_v1_static_regression.py` 可只读重算所有统计。脚本使用明确历史 roots，遇到重复 task ID 会报错，不混合后续 retry 或根目录过期 aggregate。

- 逐组指标及命中 ID：`analyse/v1_static_regression_evidence_20260918.json`
- 审计代码：`analyse/audit_v1_static_regression.py`
- 历史独立诊断：`analyse/case1_investigation_20260914.md`（不是本次新运行结果）
- 修复：`src/spreadsheet_harness/arms.py`
- 测试：`tests/test_arms.py`、`tests/test_spreadsheetbench_v1.py`
