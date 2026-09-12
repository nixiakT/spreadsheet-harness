#!/usr/bin/env zsh
set -euo pipefail
cd /data/zju-160/tongzeyuan/spreadsheet-harness
source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
export OPENAI_BASE_URL=http://47.96.153.159:8010/v1
root=benchmarks/results/trace2skill-v2-qwen36-35b-a3b-30-compatfix-20260905
logdir=tmp/trace2skill_qwen36_compatfix_logs_20260905
mkdir -p "$root" "$logdir"/cli_skill_preloaded_agent_{Debugging,Financial_Model,Template}
exec .venv/bin/python tmp/paper_repos/Trace2Skill/run_spreadsheetbench.py \
  --data_path tmp/trace2skill_spreadsheetbench_v2_30 \
  --output_dir "$root/outputs" --working_dir "$root/work" \
  --agent cli_skill_preloaded \
  --skills_dir tmp/paper_repos/Trace2Skill/spreadsheet_agent/skills \
  --model qwen36-35b-a3b --llm_client openai \
  --max_turns 30 --temperature 1 --seeds 41 --workers 4 \
  --generation_config '{"max_tokens":8192,"extra_body":{"enable_thinking":false}}' \
  --log_dir "$logdir" --log_format markdown \
  --results_file "$root/runner_results.json" --verbose
