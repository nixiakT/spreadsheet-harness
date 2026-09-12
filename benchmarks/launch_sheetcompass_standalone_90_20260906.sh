#!/usr/bin/env zsh
set -euo pipefail
cd /data/zju-160/tongzeyuan/spreadsheet-harness
source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
export OPENAI_API_KEY OPENAI_BASE_URL=http://47.96.153.159:8010/v1
exec .venv/bin/python tools/sheetcompass_standalone_runner.py
