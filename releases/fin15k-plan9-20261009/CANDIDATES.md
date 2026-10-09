# 9 个插件级优化候选（未实测晋升）

50／200／500 条 Fin15k 开发轨迹 × h-only／d-only／joint。每格独立从同一个新冻结的
Financial 基线出发；并非复用旧 skill-only proposal，也不串行混入其他格的改动。
原候选设计基线 revision：`522324bf96fe720993ab1b45ae7d533463e179885040f51220fa5692108c9dbd`。
发布运行基线统一应用 `trusted-execution-skips-bwrap-v1` 兼容修复后为
`d5a20b37919d3328910a91c4890e05b0e8e13d9868372a4c84d65e8e4477a5cf`；该修复只使
`require_isolation=False` 明确走 trusted cwd/rlimit，严格隔离路径保持不变。

## 已核验的开发失败

| 轨迹数 | 通过／失败 | 首错为非目标空白被写 | 请求修改全对但仍失败 | runtime 通过仍失败 |
|---|---:|---:|---:|---:|
| 50 | 17／33 | 10 | 6 | 31 |
| 200 | 81／119 | 45 | 27 | 113 |
| 500 | 217／283 | 111 | 60 | 267 |

统计是失败模式与事件共现，不把所有任务归咎于同一个代码点。每项 mutation 引用
本规模当前失败 trace SHA256，完整引用在 proposal、trace-audit 和 design-findings 中。
所有用于设计的反馈均来自开发集；未读取 SpreadsheetBench v2 held-out 任务答案。

## 候选矩阵

| 候选 | 项数 | 重点 | 状态 |
|---|---:|---|---|
| fin15k-plan9-50-h-only-c01 | 3 | 成功后只读复查、停用无范围补洞、工作簿边界观察及类型约束 | 未评分 |
| fin15k-plan9-50-d-only-c01 | 3 | 有见证的金融修复、唯一标签/表、类型/符号与期间观察 | 未评分 |
| fin15k-plan9-50-joint-c01 | 6 | 同时修改两组实现、技能并接入两个可执行观察插件 | 未评分 |
| fin15k-plan9-200-h-only-c01 | 4 | 成功后只读复查、停用无范围补洞、工作簿边界观察及类型约束 | 未评分 |
| fin15k-plan9-200-d-only-c01 | 3 | 有见证的金融修复、唯一标签/表、类型/符号与期间观察 | 未评分 |
| fin15k-plan9-200-joint-c01 | 7 | 同时修改两组实现、技能并接入两个可执行观察插件 | 未评分 |
| fin15k-plan9-500-h-only-c01 | 5 | 成功后只读复查、停用无范围补洞、工作簿边界观察及类型约束 | 未评分 |
| fin15k-plan9-500-d-only-c01 | 3 | 有见证的金融修复、唯一标签/表、类型/符号与期间观察 | 未评分 |
| fin15k-plan9-500-joint-c01 | 8 | 同时修改两组实现、技能并接入两个可执行观察插件 | 未评分 |

200 规模增加分句及歧义守卫、独立终验规则；500 增加跨表期间键与 terminal 边界，
并有界增加 compact profile 覆盖。新增插件已注册启用并从候选自身源码运行。
观察 hook 只能读取当前工作簿，不伪称能读取原始输入或恢复所有非目标单元格。

只读复核属于附加保护，不覆盖已完成的主执行结果。若复核只列出工作表、没有读取
实时目标内容、输出格式无效，或没有给出带精确坐标的具体缺陷，则记录为
`inconclusive` 并保留原提交；只有具备实时内容与精确目标缺陷的复核才触发修复。

## 必须实测的接纳条件

先在开发集做原协议、同模型、同预算的 paired replay／transfer／regression；按工作簿
family 隔离 transfer 与 regression，只有严格正增益且不破坏回归的候选才晋升。
之后冻结候选、一次性评估 v2；不能读 v2 失败后继续调候选，也不能提前承诺提升。
本轮仅构建与离线测试，模型调用和正式评测调用均为 0。

旧 GLM v2 Financial 59／297 的最新结果口径含 102 个 provider-failure salvage，且
旧预算/源码与当前不同，只能作参考，不能当公平的 paired baseline。旧 DeepSeek
Table1 常数也与现存汇总不同，必须明确来源和 fingerprint。

评测前还需修正现有 paired adapter 的 finite-score-only 标记与失败缓存验收：只有
`status=completed` 且 `outcome_kind=scored` 才视为正常配对；salvage 另列，不能晋升。
新实验使用全新 cache，绑定 provider／模型／generation／timeout／retry／预算及
dataset／evaluator SHA。不要复用工作区源码优先的旧 GLM 补跑脚本。

每个 candidate artifact 与 composition 已有哈希校验；仍为待评测候选，未修改线上
默认组合、旧实验结果或已有后台进程。
