# V1 隔离修复与验收（2026-09-18）

目标是提高 V1、保持 V2 默认执行行为不变。超过论文表中方法是待验证目标，不是实现保证；不改官方 comparator、gold、任务分母或历史结果。

## 三种显式模式

`benchmark v1-compare --v1-execution-mode {legacy,repaired,direct}`：

- `legacy`：默认。保留既有 V1/V2 行为和 legacy manifest，防止旧实验 resume 时混入新策略。
- `repaired`：只对 V1 开启 YAML 字符保真、正确识别公式比较符、通用 planner 写入后继续 executor。保留 planner 架构，便于独立消融。
- `direct`：V1 插件候选。保留 profile/selected skills/公式运行时校验，把全额 50 turn 预算交给直接执行器；不运行 planner 自动写入和金融/debugging 自动前后处理。要求每次 mutation 自包含、读取当前 workbook、动态推导数据范围，避免只对 case 1 有效的硬编码。Bare 路径保持不变。

修复不读取 answer_position/gold 来选择运行策略。Financial 在这些通用 V1 任务上可能与 Basic 选中相同技能，不应为了让标签看起来不同而强塞金融修复。

`repaired/direct` manifest 记录 execution mode 和 arms/runner 源码 hash；改源码后不能静默 resume。新 candidate 输出用新目录，默认不自动晋升。

## V2 保护

上一轮通用修复已收敛为 V1-only 参数。V2 CLI 没有该参数，`run_arm` 对带 V2 category 的非 legacy 模式直接拒绝。`debugging_repairs.py`、V2 comparator、V2 skills 本次未修改。

`tools/audit_v2_execution_isolation.py` 对照 commit `e0c3e0011d7ab5da938d18aa09e454c282293a02` 的 `arms.py`：

- 3 arms × 4 V2 categories 的 synthetic workbook/mock-model 执行请求、tools、skills、阶段、预算和单元格结果一致。
- V1 direct 模式中的 Bare 对照也一致。
- 证据：`analyse/v2_execution_isolation_20260918.json`。

这是执行隔离测试，不是 V2 全量准确率重跑，也不保证提供商重新采样结果完全相同。没有将候选自动推广到 V2。

## 在线 canary

启动器：`benchmarks/run_v1_isolated_canary_20260918.py`。

固定选择：排除旧 representative 200，从官方 dataset 中按 instruction_type 分层、task ID 加固定 salt 的 SHA-256 排序，每层取 2 个。只排除空指令/缺输入或 gold 文件，不读取答案内容、不按成绩筛题。

任务：Cell-Level `17111, 32902`；Sheet-Level `200-27, 228-19`。这是 development canary，非新 held-out benchmark。

运行目录：`benchmarks/results/v1-isolated-direct-canary-20260918-r2`。源码与 skills 冻结于该目录；三臂共享 model=`DeepSeek-V4-Flash`、原 endpoint、seed=41、temperature=0、top_p=1、thinking、50 calls/turns、8192 output tokens。为限制试跑费用，task timeout=1800 秒、request timeout=300 秒、retries=2（不同于旧全量的更长超时，不能冒称严格复现旧分数）。并发 2。

初次准备目录 `v1-isolated-direct-canary-20260918` 因 skill root 重复注册在模型调用前失败，原目录保留；r2 移除重复注册后启动。该启动失败不是模型分数。

在线已观测原 endpoint 返回 `429 No deployments available`，`32902/Bare` 和 `32902/Basic` 因 ProviderError 未评分。保留失败记录、不换 alias，不用这种不完整配对判断候选优劣。runner 会继续这个有限 canary，不启动全量。

获取当前汇总：

```bash
.venv/bin/python benchmarks/run_v1_isolated_canary_20260918.py \
  --output benchmarks/results/v1-isolated-direct-canary-20260918-r2 --summarize
```

汇总用固定预选任务分母并显示 scored/缺失/API 错误；只有三臂所有任务都 scored 才允许质量比较。即使 canary 全通过也不会自动晋升，更不能据四题声称超过全量基线。

## 验收边界

已完成核心/CLI/V1/V2/plugin/financial/Harbor 回归测试 273 项，加上 canary 分母/失败晋升保护测试 2 项，共 **275 项通过**。下一步先确认在线 canary、隔离服务错误，再做更大的固定任务配对实验和重复运行；最终以与其他方法同一任务列表、同一预算的全量 Soft/Hard 验证。截图中 DeepSeek 最优 Soft=52.89、Hard=46.15（来自不同方法），Qwen=38.01/31.58，只能作为目标参照，不能和此 4-task canary 直接排名。
