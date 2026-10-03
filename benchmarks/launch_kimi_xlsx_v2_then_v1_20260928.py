"""Run Kimi + official xlsx: V2 first, then V1, parallelism 30 per group."""
from __future__ import annotations
import hashlib, json, shlex, subprocess, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'benchmarks/results/kimi-xlsx-v2-then-v1-20260928'
SOCKET = 'kimi-xlsx-v2-then-v1-20260928'
PY = ROOT / '.venv/bin/python'
MODEL = 'dashscope/kimi-k2.7-code'
BASE = 'http://10.130.138.46:8010/v1'
KEY = '/tmp/spreadsheet-harness-litellm.key'
SKILL = ROOT / 'skills/xlsx/SKILL.md'

def launch(version: str, harness: str):
    name = f'{version}-{harness}'
    root = OUT / name
    log = OUT / f'{name}.log'
    if version == 'v1':
        runner = ROOT / 'benchmarks/run_external_harness_spreadsheetbench_v1.py'
        cmd = [str(PY), '-u', str(runner), '--harness', harness, '--model', MODEL,
               '--run-root', str(root), '--dataset', str(ROOT/'benchmarks/data/spreadsheetbench_912_v0.1'),
               '--skill', str(SKILL), '--parallelism', '30', '--max-turns', '50',
               '--max-output-tokens', '32768', '--task-timeout', '21600', '--replay-timeout', '1800',
               '--base-url', BASE, '--api-key-file', KEY]
    else:
        runner = {'codex':'run_codex_spreadsheetbench_v2.py','claude':'run_claude_spreadsheetbench_v2.py','dsh':'run_deepseek_harness_spreadsheetbench_v2.py'}[harness]
        cmd = [str(PY), '-u', str(ROOT/'benchmarks'/runner), '--dataset', str(ROOT/'benchmarks/data/spreadsheetbench-v2'),
               '--run-root', str(root), '--skill', str(SKILL), '--parallelism', '30', '--max-turns', '50',
               '--task-timeout', '21600', '--model', MODEL, '--base-url', BASE, '--api-key-file', KEY,
               '--recalculate-before-evaluation']
    shell = f'cd {shlex.quote(str(ROOT))} && exec {shlex.join(cmd)} >> {shlex.quote(str(log))} 2>&1'
    subprocess.run(['tmux','-L',SOCKET,'new-session','-d','-s',name,shell],check=True)
    print('started',name,flush=True)

def main():
    if OUT.exists(): raise SystemExit(f'existing output root: {OUT}')
    OUT.mkdir(parents=True)
    manifest={'model':MODEL,'skill':str(SKILL),'skill_sha256':hashlib.sha256(SKILL.read_bytes()).hexdigest(),
              'base_url':BASE,'parallelism':30,'max_turns':50,'temperature':0,'top_p':1,
              'thinking':True,'reasoning_effort':'medium','order':'v2_then_v1','no_skill':'deferred'}
    (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    for h in ('codex','claude','dsh'): launch('v2',h)
    while True:
        active=set(subprocess.check_output(['tmux','-L',SOCKET,'list-sessions','-F','#S'],text=True).split())
        v2={f'v2-{h}' for h in ('codex','claude','dsh')}
        if not (active & v2): break
        time.sleep(30)
    for h in ('codex','claude','dsh'): launch('v1',h)
    print('V2 complete; V1 started',flush=True)

if __name__=='__main__': main()
