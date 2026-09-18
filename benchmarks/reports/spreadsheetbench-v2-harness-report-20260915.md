# SpreadsheetBench-v2 Harness Report

统计日期：2026-09-15（Asia/Shanghai）

## 统一配置

- 数据集：SpreadsheetBench-v2，321 题
- 最大模型调用/步数：50
- 并发度：6
- reasoning effort：medium
- temperature：0
- top_p：1
- thinking：开启
- LiteLLM：内网 endpoint `http://10.130.138.46:8010/v1`
- 非 Visualization 指标：Accuracy、Modification Accuracy、Regression Accuracy
- Visualization：使用独立视觉评测器，不纳入上述三项总平均

## 评分结果

下表顺序为 `Accuracy / Modification Accuracy / Regression Accuracy`。

| 模型 | Harness | Skill | Debugging | Financial_Model | Template | Visualization | 总计 |
|---|---|---|---|---|---|---|---|
| DeepSeek-V4-Flash | Codex | spreadsheet-manipulation | 100/100：2.00% / 55.18% / 99.25% | 100/100：16.00% / 81.15% / 99.56% | 97/97：20.62% / 75.83% / 98.67% | 24/24 | 321/321：**12.79% / 70.67% / 99.17%** |
| DeepSeek-V4-Flash | Claude Code | spreadsheet-manipulation | 100/100：3.00% / 52.98% / 99.30% | 100/100：18.00% / 80.97% / 99.50% | 97/97：15.46% / 73.16% / 98.63% | 22/24 | 319/321：**12.12% / 69.00% / 99.15%** |
| DeepSeek-V4-Flash | Codex | spreadsheet-core | 97/100：2.06% / 54.24% / 99.04% | 100/100：17.00% / 79.75% / 99.49% | 97/97：16.49% / 73.52% / 98.47% | 24/24 | 318/321：**11.90% / 69.28% / 99.00%** |
| DeepSeek-V4-Flash | Claude Code | spreadsheet-core | 99/100：2.02% / 52.07% / 99.21% | 100/100：18.00% / 78.77% / 99.37% | 97/97：17.53% / 73.87% / 98.77% | 23/24 | 319/321：**12.50% / 68.23% / 99.12%** |
| DeepSeek-V4-Flash | DeepSeekHarness (`dsh`) | spreadsheet-core | 99/100：1.01% / 47.55% / 99.42% | 99/100：18.18% / 78.80% / 99.54% | 97/97：19.59% / 73.54% / 98.78% | 23/24 | 318/321：**12.88% / 66.58% / 99.25%** |
| Qwen3-Coder-480B | Codex | spreadsheet-core | 94/100：0.00% / 41.64% / 99.43% | 94/100：0.00% / 36.18% / 99.51% | 97/97：2.06% / 29.95% / 97.34% | 24/24 | 309/321：**0.70% / 35.86% / 98.75%** |
| Qwen3-Coder-480B | Claude Code | spreadsheet-core | 100/100：0.00% / 45.98% / 99.17% | 100/100：1.00% / 46.69% / 98.81% | 97/97：2.06% / 40.67% / 97.77% | 24/24 | 321/321：**1.01% / 44.49% / 98.59%** |
| Qwen3-Coder-480B | DeepSeekHarness (`dsh`) | spreadsheet-core | 76/100：0.00% / 45.02% / 99.15% | 83/100：1.20% / 43.11% / 99.48% | 76/97：3.95% / 34.97% / 96.09% | 24/24 | 259/321：**1.70% / 41.10% / 98.28%** |

## 步数、请求和运行状态

`model_requests/turns` 是结果文件中记录的模型请求数；它不是工具调用数。

| 实验 | 总模型请求 | 平均/题 | 最大/题 | 完成状态 |
|---|---:|---:|---:|---|
| DeepSeek + Codex + manipulation | 6,052 | 18.85 | 50 | 321 completed |
| DeepSeek + Claude + manipulation | 6,038 | 18.81 | 50 | 319 completed，2 turn-limit |
| DeepSeek + Codex + core | 6,817 | 21.24 | 50 | 318 completed，3 turn-limit |
| DeepSeek + Claude + core | 8,046 | 25.07 | 50 | 319 completed，2 turn-limit |
| DeepSeek + DeepSeekHarness + core | 8,400 | 26.17 | 50 | 318 completed，3 turn-limit |
| Qwen + Codex + core | 3,756 | 11.70 | 50 | 309 completed，12 turn-limit |
| Qwen + Claude + core | 7,966 | 24.82 | 50 | 321 completed |
| Qwen + DeepSeekHarness + core | 13,017 | 40.55 | 57 | 259 completed，60 turn-limit，2 failed |

## Token 与工具调用日志

Codex proxy 能直接提供 token 和响应工具调用统计；DeepSeekHarness proxy 能提供请求、重试、错误和限额统计。旧 Claude 结果主要把 token 放在每题 trajectory 的 `result.usage` 中，未统一写入顶层结果汇总，因此不能直接和 Codex/DSH 的 proxy 数字混用。

已可靠读到的 proxy 汇总：

| 实验 | Prompt/Input tokens | Completion/Output tokens | Reasoning tokens | 响应工具调用数 | Proxy 错误 | 上游重试 | 限额拦截 |
|---|---:|---:|---:|---:|---:|---:|---:|
| DeepSeek + Codex + manipulation | 257,255,300 | 16,331,126 | 13,515,014 | 6,243 | 0 | 0 | 4 |
| DeepSeek + Codex + core | 245,405,402 | 14,084,616 | 11,138,575 | 6,448 | 0 | 0 | 7 |
| Qwen + Codex + core | 51,887,708 | 870,331 | 0 | 2,259 | 0 | 0 | 0 |
| DeepSeek + DeepSeekHarness + core | 未由上游返回 token | 未由上游返回 token | 未由上游返回 token | 不适用 | 26 | 4,694 | 66 |
| Qwen + DeepSeekHarness + core | 未由上游返回 token | 未由上游返回 token | 未由上游返回 token | 不适用 | 105 | 9,386 | 2,322 |

说明：`限额拦截`是 proxy 看到的超额请求事件，不等同于最终 turn-limit 题数。DSH 的请求、429 重试和成功请求分别记录，严格步数应以成功 upstream response 计数。

## 现象分析

1. DeepSeek 模型在三种 harness 上的 Accuracy 都约 11.9%–12.9%，明显高于 Qwen 的 0.7%–1.7%。
2. DeepSeekHarness 的 Regression 最好或接近最好（DeepSeek 99.25%），但 Qwen + DeepSeekHarness 消耗最多请求，且 turn-limit 最多。
3. Qwen + Codex 平均请求最少（11.70/题），但 Modification 只有 35.86%，说明较早结束并没有带来有效修复。
4. Qwen + Claude 的完成率最高（321/321），但 Accuracy 仍只有 1.01%，说明“完成任务协议”与“修对目标单元格”是两个不同问题。
5. 所有组合 Regression 都很高（约 98.28%–99.56%），说明保留未涉及内容的能力整体稳定；主要瓶颈是目标修改正确率和公式/数值推理。
6. 旧 manipulation 运行的 `elapsed_seconds` 不能直接做速度比较，因为部分结果包含续跑和等待时间；请求数和 proxy 时间戳更适合做效率比较。

## 结果来源

- `benchmarks/results/deepseek-v4-flash-codex-real-v2-full-20260911`
- `benchmarks/results/deepseek-v4-flash-claude-code-real-v2-full-20260912`
- `benchmarks/results/deepseek-v4-flash-codex-core-full-v1-20260914`
- `benchmarks/results/deepseek-v4-flash-claude-core-full-v1-20260914`
- `benchmarks/results/deepseek-v4-flash-official-dsh-full-v2-20260913`
- `benchmarks/results/qwen3-coder-480b-a35b-instruct-codex-full-v1-20260913`
- `benchmarks/results/qwen3-coder-480b-a35b-instruct-claude-full-v1-20260913`
- `benchmarks/results/qwen3-coder-480b-a35b-instruct-deepseek-full-v1-20260913`

