#!/usr/bin/env zsh
set -euo pipefail

cd /data/zju-160/tongzeyuan/spreadsheet-harness

zsh benchmarks/launch_full_v2_nonvisual_after_canary.sh 0
zsh benchmarks/launch_full_v2_visual_after_nonvisual.sh 0
zsh benchmarks/launch_full_v1_after_v2.sh 0
zsh benchmarks/launch_full_verified_after_v1.sh 0
