# Spreadsheet Agent Harness

一个面向真实 Excel 工作簿任务的 Linux-first agent harness。项目从零
clean-room 构建，提供隔离执行、表格工具、模型调用、轨迹记录和官方评测，
并在固定内核上支持插件组合与演化。

## 核心思想

通用 Excel 任务并不需要每次从头生成一套 agent。我们提供一个相对稳定的
harness 内核，再根据任务或实验需要组合不同插件。插件可以增补能力、知识、
验证或修复策略，但不能绕过内核的安全边界和评测语义。

```text
固定 harness 内核
  ├─ agent loop 与 provider adapter
  ├─ 工具执行与 Bubblewrap sandbox
  ├─ WorkbookSession 与原子化修改
  ├─ trajectory / manifest 记录
  └─ SpreadsheetBench 评分器
           │
           ▼
插件组合解析（CompositionSpec + composition hash）
           │
           ▼
observe / act / control / knowledge / verify / repair / workflow
```

这种设计借鉴了插件式 agent 的思想，也与 JIT-Agent 等“按能力组合和演化”
的方向相近；但本项目不是 DeepSeekHarness、JIT-Agent 或其他项目的代码复制，
也不依赖某个特定框架（例如 Cordis）。

## 三种主要 arm

- `bare`：基础 agent 流程，使用通用表格工具，不启用额外领域插件。
- `spreadsheet-harness-basic`：启用基础表格操作、公式处理和验证相关插件。
- `spreadsheet-harness-financial`：在 basic 的基础上启用财务模型插件和
  financial runtime。

每个 arm 都解析为一个 `CompositionSpec`。插件版本、能力、配置和组合 hash
会写入运行结果，保证不同组合可以审计和复现。

## 插件模型

`src/spreadsheet_harness/plugins.py` 定义插件契约，包括版本、实现标识、
能力依赖、hooks、权限、配置字段、冲突关系和演化策略。

插件可在任务开始前、模型请求前、工具调用后、提交前和运行结束后介入。
`capability_evolution.py` 根据失败证据区分基础设施、组合和插件局部问题，
支持候选插件的选择、演化、回放验证、提升或回滚。

当前插件主要在仓库内静态注册，由代码统一控制契约、解析和生命周期；它不是
可以任意动态加载外部 Python 插件的通用插件市场。

## 安装

需要 Python 3.10+ 和 LibreOffice：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
sheet-harness doctor
```

模型和 endpoint 可通过 `SHEET_AGENT_BASE_URL`、`SHEET_AGENT_MODEL` 和
`OPENAI_API_KEY` 配置。

## 使用

运行单个工作簿任务：

```bash
sheet-harness run workbook.xlsx \
  --instruction 'Add a Total formula to D2:D20.' \
  --runs-dir runs
```

运行 SpreadsheetBench v2 对比：

```bash
sheet-harness benchmark v2-compare \
  --dataset benchmarks/data/spreadsheetbench-v2 \
  --category Template --task-id Template/01_01 \
  --arm bare --arm spreadsheet-harness-basic \
  --arm spreadsheet-harness-financial \
  --output benchmarks/results/example \
  --model MODEL --api-protocol chat-completions
```

每次运行会保存输入快照、`artifacts/output.xlsx`、`trajectory.jsonl`、
`run.json` 和评测结果。`status=completed` 只表示执行完成，正确性应以
`official_score` 为准。

Financial calibration bundles from the v0.6 generator and the enhanced v2
release use Harbor's one-task-per-directory layout.  They can be passed to
the same v2 runner directly; the harness safely materializes only the task
metadata and input/golden workbook pair:

```bash
sheet-harness benchmark v2-compare \
  --dataset benchmarks/data/SpreadsheetBench-v2-enhanced-Financial_Model-1565.tar.gz \
  --category Financial_Model \
  --task-id Financial_Model/fina_Fina_dam_12ec3ea6c4_fcff2st_c0 \
  --arm spreadsheet-harness-financial \
  --max-model-calls 50 --max-turns-per-arm 50 \
  --max-output-tokens unlimited \
  --temperature 0 --top-p 1 --enable-thinking \
  --output benchmarks/results/financial-smoke
```

`unlimited`（也可写 `0` 或 `none`）表示不向 provider 发送 `max_tokens`；总
model-call、total-token 和 task-timeout budget 仍然生效。

Use `benchmark harbor-normalize SOURCE --output DIR` when a persistent
canonical `Financial_Model/dataset.json` is needed.  Both supplied archives
are marked `dataset_role=calibration_only`; keep them out of held-out claims.

四臂 co-evolution 结果可以用同一个只读汇总器生成 paired win/tie/loss、资源成本
以及 `G_H`、`G_D`、`G_joint`、`I`。它按任务目录读取 `summary.json`，未完成的 arm
不会被静默当成 0：

```bash
.venv/bin/python tools/report_coevolution_metrics.py \
  --arm h0d0=benchmarks/results/coevolution-validation/h0d0 \
  --arm h1d0=benchmarks/results/coevolution-validation/h1d0 \
  --arm h0d1=benchmarks/results/coevolution-validation/h0d1 \
  --arm h1d1=benchmarks/results/coevolution-validation/h1d1 \
  --metric accuracy \
  --output benchmarks/results/coevolution-validation/report.json \
  --markdown benchmarks/results/coevolution-validation/report.md
```

查看插件组合或生成候选 skill：

```bash
sheet-harness plugins list
sheet-harness plugins resolve spreadsheet-harness-basic
sheet-harness evolve generate runs/*/trajectory.jsonl --output evolution
```

## 开发

```bash
pytest
ruff check .
```

benchmark 结果必须同时记录数据集、模型、provider protocol、计算后端、插件
组合 hash 和代码/skill manifest；不同协议或计算引擎的结果不能直接混成同一
排行榜数字。

### 插件级持续进化

`evolve generate/promote` 是旧的单个 skill 候选流程；完整的插件自进化使用持久化、
契约约束的控制器。它可更新插件的 prompt、description、bounded config 和实现文件，
在隔离 revision 中完成候选构建，按 replay/transfer/regression 三种配对上下文验证，
通过 bootstrap 置信下界后才原子晋升，并支持断点恢复、回滚和 held-out 冻结：

```bash
sheet-harness evolve continuous-init config.json evolution-workspace
sheet-harness evolve continuous-evidence config.json evolution-workspace evidence.json
sheet-harness evolve continuous-run config.json evolution-workspace --rounds 10
sheet-harness evolve continuous-status evolution-workspace
sheet-harness evolve continuous-freeze config.json evolution-workspace
```

配置中的 `proposer_command` 和 `evaluator_command` 是受控适配器（使用
`{request}`、`{response}`、`{directory}` 占位符）。控制器固定契约、任务划分、评估绑定、
阈值和状态迁移；适配器只能返回结构化候选或验证报告，不能扩大写入权限。要将已晋升
revision 用于 SpreadsheetBench-v2，可把其 `composition.json` 传给
`benchmark v2-compare --composition-file ours=... --skill-root revision/artifact/skills`；金融模型插件的实现坐标还包括
`src/spreadsheet_harness/financial_model_repairs.py`，因此不再局限于 `SKILL.md`。
可直接复制并填写 [`examples/continuous_evolution_config.example.json`](examples/continuous_evolution_config.example.json)。
