#!/usr/bin/env zsh
set -euo pipefail

# Clean-room execution of the public Trace2Skill spreadsheet agent with its
# released xlsx-35B skill on the same fixed 30-case SpreadsheetBench-v2 split
# used by the local harness comparisons.  The model alias is the lab's
# qwen36-35b-a3b endpoint; it is not the paper's Qwen3.5-35B checkpoint.
cd /data/zju-160/tongzeyuan/spreadsheet-harness
source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
export OPENAI_BASE_URL=http://47.96.153.159:8010/v1

.venv/bin/python tools/prepare_trace2skill_spreadsheetbench_v2.py
root=benchmarks/results/trace2skill-v2-qwen36-35b-a3b-30-skill-preloaded-20260905
mkdir -p "$root" tmp/trace2skill_logs_20260905/cli_skill_preloaded_agent_Debugging \
  tmp/trace2skill_logs_20260905/cli_skill_preloaded_agent_Financial_Model \
  tmp/trace2skill_logs_20260905/cli_skill_preloaded_agent_Template

exec tmp/paper_repos/Trace2Skill/../../../.venv/bin/python \
  tmp/paper_repos/Trace2Skill/run_spreadsheetbench.py \
  --data_path tmp/trace2skill_spreadsheetbench_v2_30 \
  --output_dir "$root/outputs" \
  --working_dir "$root/work" \
  --agent cli_skill_preloaded \
  --skills_dir tmp/paper_repos/Trace2Skill/spreadsheet_agent/skills \
  --model qwen36-35b-a3b --llm_client openai \
  --max_turns 100 --temperature 1 --seeds 41 --workers 2 \
  --log_dir tmp/trace2skill_logs_20260905 --log_format markdown \
  --results_file "$root/runner_results.json" \
  --verbose
