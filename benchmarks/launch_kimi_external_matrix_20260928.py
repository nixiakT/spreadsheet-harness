"""Launch the isolated Kimi external-harness matrix once, without retry loops."""
import hashlib
import json
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'benchmarks/results/kimi-external-matrix-20260928'
SOCKET = 'kimi-external-matrix-20260928'
MODEL = 'dashscope/kimi-k2.7-code'

def main():
    if OUT.exists():
        raise SystemExit(f'Refusing to overwrite existing matrix: {OUT}')
    OUT.mkdir(parents=True)
    specs = []
    for version in ('v1', 'v2'):
        for harness in ('codex', 'claude', 'dsh'):
            for condition in ('no-skill', 'xlsx'):
                name = f'{version}-{harness}-{condition}'
                runner = 'run_external_harness_spreadsheetbench_v1.py' if version == 'v1' else {
                    'codex': 'run_codex_spreadsheetbench_v2.py',
                    'claude': 'run_claude_spreadsheetbench_v2.py',
                    'dsh': 'run_deepseek_harness_spreadsheetbench_v2.py',
                }[harness]
                cmd = [str(ROOT / '.venv/bin/python'), '-u', str(ROOT / 'benchmarks' / runner),
                       '--model', MODEL, '--run-root', str(OUT / name),
                       '--dataset', str(ROOT / 'benchmarks/data' / ('spreadsheetbench_912_v0.1' if version == 'v1' else 'spreadsheetbench-v2')),
                       '--parallelism', '6', '--max-turns', '50', '--task-timeout', '21600',
                       '--base-url', 'http://10.130.138.46:8010/v1',
                       '--api-key-file', '/tmp/spreadsheet-harness-litellm.key']
                cmd += ['--no-skill'] if condition == 'no-skill' else ['--skill', str(ROOT / 'skills/xlsx/SKILL.md')]
                if version == 'v1':
                    cmd += ['--harness', harness, '--max-output-tokens', '32768', '--replay-timeout', '1800']
                else:
                    cmd += ['--recalculate-before-evaluation']
                specs.append({'name': name, 'command': cmd, 'expected': 912 if version == 'v1' else 321})
    plan = {'model': MODEL, 'temperature': 0, 'top_p': 1, 'thinking_requested': True,
            'reasoning_effort_requested': 'medium', 'parallelism_per_group': 6,
            'max_turns': 50, 'total_tasks': sum(s['expected'] for s in specs),
            'skill_sha256': hashlib.sha256((ROOT / 'skills/xlsx/SKILL.md').read_bytes()).hexdigest(),
            'groups': specs}
    (OUT / 'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    for spec in specs:
        log = OUT / (spec['name'] + '.log')
        shell = f'cd {shlex.quote(str(ROOT))} && exec {shlex.join(spec["command"])} >> {shlex.quote(str(log))} 2>&1'
        subprocess.run(['tmux', '-L', SOCKET, 'new-session', '-d', '-s', spec['name'], shell], check=True)
        print('started', spec['name'], flush=True)

if __name__ == '__main__':
    main()
