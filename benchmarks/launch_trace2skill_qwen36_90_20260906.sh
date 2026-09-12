#!/usr/bin/env zsh
set -euo pipefail
cd /data/zju-160/tongzeyuan/spreadsheet-harness
source /data/zju-160/tongzeyuan/.config/litellm/lab.env
export OPENAI_API_KEY="$OPENAI_API_KEY"
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
export OPENAI_BASE_URL=http://47.96.153.159:8010/v1
root=${TRACE_ROOT:-benchmarks/results/trace2skill-qwen36-90-20260906}
mkdir -p "$root"
exec .venv/bin/python tmp/paper_repos/Trace2Skill/run_spreadsheetbench.py \
 --data_path tmp/trace2skill_spreadsheetbench_v2_90 --output_dir "$root/outputs" --working_dir "$root/work" \
 --agent cli_skill_preloaded --skills_dir tmp/paper_repos/Trace2Skill/spreadsheet_agent/skills \
 --model qwen36-35b-a3b --llm_client openai --max_turns 20 --temperature 1 --seeds 41 --workers 4 \
 --generation_config '{"max_tokens":8192,"extra_body":{"enable_thinking":false}}' \
 --log_dir "$root/logs" --log_format markdown --results_file "$root/runner_results.json" --verbose
