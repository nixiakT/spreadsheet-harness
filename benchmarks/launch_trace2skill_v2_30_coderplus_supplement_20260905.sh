#!/usr/bin/env zsh
set -euo pipefail
cd /data/zju-160/tongzeyuan/spreadsheet-harness
source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
export PATH="/data/zju-160/tongzeyuan/spreadsheet-harness/.venv/bin:$PATH"
mkdir -p tmp/trace2skill_coderplus_envfixed_logs_20260905/cli_skill_preloaded_agent_Debugging \
  tmp/trace2skill_coderplus_envfixed_logs_20260905/cli_skill_preloaded_agent_Financial_Model \
  tmp/trace2skill_coderplus_envfixed_logs_20260905/cli_skill_preloaded_agent_Template \
  benchmarks/results/trace2skill-v2-dashscope-qwen3-coder-plus-30-envfixed-20260905
exec .venv/bin/python tmp/paper_repos/Trace2Skill/run_spreadsheetbench.py \
  --data_path tmp/trace2skill_spreadsheetbench_v2_30 \
  --output_dir benchmarks/results/trace2skill-v2-dashscope-qwen3-coder-plus-30-envfixed-20260905/outputs \
  --working_dir benchmarks/results/trace2skill-v2-dashscope-qwen3-coder-plus-30-envfixed-20260905/work \
  --agent cli_skill_preloaded \
  --skills_dir tmp/paper_repos/Trace2Skill/spreadsheet_agent/skills \
  --model dashscope/qwen3-coder-plus --llm_client openai \
  --max_turns 100 --temperature 1 --seeds 41 --workers 4 \
  --log_dir tmp/trace2skill_coderplus_envfixed_logs_20260905 --log_format markdown \
  --results_file benchmarks/results/trace2skill-v2-dashscope-qwen3-coder-plus-30-envfixed-20260905/runner_results.json \
  --verbose
