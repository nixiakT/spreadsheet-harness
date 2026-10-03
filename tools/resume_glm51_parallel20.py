"""Scoped concurrency migration; retain task records and cumulative proxy budgets."""
import json
import os
import shlex
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import psutil

REPO = Path(__file__).resolve().parents[1]
ROOT = REPO / 'benchmarks/results/glm51-xlsx-v2-then-v1-20260928'
SOCKET = 'glm51-parallel20-20260928'
HARNESSES = ('codex', 'claude', 'dsh')


def write(path, obj):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


def main():
    stamp = datetime.now().strftime('%Y%m%dT%H%M%S')
    backup = ROOT / ('parallel20-migration-' + stamp)
    backup.mkdir()
    commands = {}
    runners = []
    for p in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            a = p.info['cmdline'] or []
            if 'python' not in p.info['name'] or '--run-root' not in a:
                continue
            runroot = Path(a[a.index('--run-root') + 1])
            if runroot.parent != ROOT or runroot.name not in {f'v2-{h}' for h in HARNESSES}:
                continue
            assert a[a.index('--model') + 1] == 'GLM-5.1'
            commands[runroot.name] = list(a)
            runners.append(p)
        except psutil.NoSuchProcess:
            continue
    assert set(commands) == {f'v2-{h}' for h in HARNESSES}, commands.keys()
    write(backup / 'original-commands.json', commands)
    # Freeze only the validated runner parents before collecting descendants.
    for p in runners:
        p.suspend()
    children = [child for p in runners for child in p.children(recursive=True)]
    for p in children + runners:
        try:
            p.terminate()
            p.resume()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(children + runners, timeout=10)
    for p in alive:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass
    for name, cmd in commands.items():
        path = ROOT / name / 'manifest.json'
        shutil.copy2(path, backup / f'{name}-manifest.json')
        manifest = json.loads(path.read_text())
        assert manifest['model'] == 'GLM-5.1'
        manifest['parallelism'] = 20
        write(path, manifest)
        cmd[cmd.index('--parallelism') + 1] = '20'
        shell = f'cd {shlex.quote(str(REPO))} && exec {shlex.join(cmd)} >> {shlex.quote(str(ROOT / (name + ".log")))} 2>&1'
        subprocess.run(['tmux', '-L', SOCKET, 'new-session', '-d', '-s', name, shell], check=True)
        print('resumed', name, 'parallelism=20', flush=True)
    path = ROOT / 'manifest.json'
    shutil.copy2(path, backup / 'matrix-manifest.json')
    manifest = json.loads(path.read_text())
    manifest['parallelism'] = 20
    write(path, manifest)
    # Session exit alone does not prove V2 completion. Require all task records.
    while True:
        p = subprocess.run(['tmux', '-L', SOCKET, 'list-sessions', '-F', '#S'], capture_output=True, text=True)
        active = set(p.stdout.splitlines())
        if not active.intersection(commands):
            break
        time.sleep(30)
    for h in HARNESSES:
        records = list((ROOT / f'v2-{h}' / 'tasks').glob('*/status.json'))
        if len(records) != 321:
            print('V1 not started: incomplete V2 records', h, len(records), flush=True)
            return
    for h in HARNESSES:
        cmd = [str(REPO / '.venv/bin/python'), '-u', str(REPO / 'benchmarks/run_external_harness_spreadsheetbench_v1.py'),
               '--harness', h, '--model', 'GLM-5.1', '--run-root', str(ROOT / f'v1-{h}'),
               '--dataset', str(REPO / 'benchmarks/data/spreadsheetbench_912_v0.1'),
               '--skill', str(REPO / 'skills/xlsx/SKILL.md'), '--parallelism', '20', '--max-turns', '50',
               '--max-output-tokens', '32768', '--task-timeout', '21600', '--replay-timeout', '1800',
               '--base-url', 'http://10.130.138.46:8010/v1', '--api-key-file', '/tmp/spreadsheet-harness-litellm.key']
        shell = f'cd {shlex.quote(str(REPO))} && exec {shlex.join(cmd)} >> {shlex.quote(str(ROOT / ("v1-" + h + ".log")))} 2>&1'
        subprocess.run(['tmux', '-L', SOCKET, 'new-session', '-d', '-s', f'v1-{h}', shell], check=True)


if __name__ == '__main__':
    main()
