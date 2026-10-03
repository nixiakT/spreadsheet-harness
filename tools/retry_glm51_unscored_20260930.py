#!/usr/bin/env python3
import json
import subprocess
from pathlib import Path

ROOT = Path('/data/zju-160/tongzeyuan/spreadsheet-harness')
BASE = ROOT / 'benchmarks/results/glm51-spreadsheetagent-official-compat-t10800-r3600-20260929'
OUT = BASE / 'retry-final-20260930'
LAUNCHER = ROOT / 'benchmarks/run_glm51_spreadsheetagent_official_compat_20260928.py'

def unscored(suite: str) -> list[str]:
    ids = []
    for path in sorted((BASE / suite).glob('*/results.json')):
        data = json.loads(path.read_text())
        rows = data if isinstance(data, list) else [data]
        for row in rows:
            if row.get('status') != 'completed':
                ids.append(str(row['task_id']))
    return ids

def main() -> int:
    v1 = unscored('v1')
    v2 = unscored('v2')
    print(json.dumps({'v1_retry': len(v1), 'v2_retry': len(v2)}), flush=True)
    cmd = [str(ROOT / '.venv/bin/python'), '-u', str(LAUNCHER), '--suite', 'both',
           '--output', str(OUT), '--workers', '20', '--task-timeout', '10800',
           '--request-timeout', '3600', '--request-interval', '1.1']
    for task in v1:
        cmd += ['--v1-task', task]
    for task in v2:
        cmd += ['--v2-task', task]
    return subprocess.call(cmd, cwd=ROOT)

if __name__ == '__main__':
    raise SystemExit(main())
