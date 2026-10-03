"""Frozen, paired plugin-deletion pilot. Never reuses historical best-of results."""
from __future__ import annotations

import argparse
import collections
import dataclasses
import fcntl
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'benchmarks/results/deepseek-ablation5-20260929'
FULL_OUT = ROOT / 'benchmarks/results/deepseek-ablation-full-20260929'
DATA = ROOT / 'benchmarks/data/spreadsheetbench-v2'
PY = ROOT / '.venv/bin/python'
EVAL = ROOT / 'benchmarks/vendor/spreadsheetbench2-official-83d415c/evaluation/evaluation.py'
CATS = ('Template', 'Financial_Model', 'Debugging')
FULL_RUN = False
REMOVALS = {
    'minus_formula_knowledge': 'knowledge-formula',
    'minus_formula_runtime': 'verify-formula-runtime',
    'minus_structure_knowledge': 'knowledge-structure',
    'minus_financial_plugin': 'knowledge-financial-model',
}


def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def write(p, value):
    p.parent.mkdir(parents=True, exist_ok=True)
    temp = p.with_suffix(p.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temp.replace(p)


def prepare():
    if (OUT / 'plan.json').exists():
        return json.loads((OUT / 'plan.json').read_text())
    frozen = OUT / 'frozen'
    if frozen.exists():
        raise RuntimeError('Incomplete preparation; retain directory for audit and inspect it.')
    shutil.copytree(ROOT / 'src/spreadsheet_harness', frozen / 'src/spreadsheet_harness',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copytree(ROOT / 'skills', frozen / 'skills',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copy2(EVAL, frozen / 'evaluation.py')
    sys.path.insert(0, str(frozen / 'src'))
    from spreadsheet_harness.plugins import BUILTIN_COMPOSITIONS, CompositionSpec, default_plugin_registry, execution_plan
    registry = default_plugin_registry()
    base = BUILTIN_COMPOSITIONS['spreadsheet-harness-financial']
    variants = {}
    for label, arm in [('bare', 'bare'), ('basic', 'spreadsheet-harness-basic'), ('financial', 'spreadsheet-harness-financial')]:
        variants[label] = {'arm': arm, 'spec': BUILTIN_COMPOSITIONS[arm].to_dict()}
    for label, removed in REMOVALS.items():
        spec = CompositionSpec.create(label, [p for p in base.plugins if p != removed], dict(base.overrides))
        assert set(base.plugins) - set(spec.plugins) == {removed}
        variants[label] = {'arm': 'spreadsheet-harness-financial', 'removed': removed, 'spec': spec.to_dict()}
    for label, entry in variants.items():
        spec = CompositionSpec.create(**entry['spec'])
        resolved = registry.resolve(spec)
        execution = execution_plan(resolved)
        entry['resolved'] = resolved.to_dict()
        entry['execution'] = {f.name: dict(getattr(execution, f.name)) if f.name=='profile_config' else getattr(execution, f.name) for f in dataclasses.fields(execution)}
        write(OUT / 'compositions' / (label + '.json'), entry['spec'])
    assert not variants['minus_formula_runtime']['execution']['require_formula_runtime_validation']
    assert 'spreadsheet-formula' not in variants['minus_formula_knowledge']['execution']['skill_names']
    assert 'spreadsheet-structure' not in variants['minus_structure_knowledge']['execution']['skill_names']
    assert not variants['minus_financial_plugin']['execution']['financial_model_runtime']
    tasks, dataset_hashes = [], {}
    rng = random.Random(20260929)
    for cat in CATS:
        f = DATA / cat / 'dataset.json'
        dataset_hashes[cat] = digest(f)
        groups = collections.defaultdict(list)
        for row in json.loads(f.read_text()):
            groups[str(row['id']).split('_')[0]].append(str(row['id']))
        keys = sorted(groups)
        rng.shuffle(keys)
        for items in groups.values():
            items.sort(); rng.shuffle(items)
        selected = []
        target = sum(len(x) for x in groups.values()) if FULL_RUN else 20
        while len(selected) < target:
            for key in keys:
                if groups[key] and len(selected) < target:
                    selected.append(f'{cat}/{groups[key].pop()}')
        tasks.extend(selected)
    expected_tasks = sum(len(json.loads((DATA / cat / 'dataset.json').read_text())) for cat in CATS) if FULL_RUN else 60
    assert len(tasks) == len(set(tasks)) == expected_tasks
    # Interleave categories and configurations so provider-time effects do not
    # systematically put all anchors ahead of all ablations.
    by_cat = {cat: [t for t in tasks if t.startswith(cat + '/') ] for cat in CATS}
    ordered = [by_cat[cat][i] for i in range(max(map(len, by_cat.values())))
               for cat in CATS if i < len(by_cat[cat])]
    jobs = []
    for tid in ordered:
        labels = list(variants); rng.shuffle(labels)
        jobs.extend({'variant': v, 'task_id': tid} for v in labels)
    hashes = {str(f.relative_to(frozen)): digest(f) for f in frozen.rglob('*') if f.is_file()}
    plan = {'created_at': datetime.now(timezone.utc).isoformat(), 'pilot': True,
            'selection': 'seed=20260929; round-robin task-id prefix strata; no outcome-based selection',
            'limitations': ['Previously explored public benchmark; not untouched held-out.',
                           'No planner/profile deletion: controller dependencies prevent isolated removal.',
                           'Date-text repair only; other repair paths stay enabled.',
                           'Financial plugin deletion removes knowledge AND financial runtime.',
                           'Memory deletion removes skill text, not a persistent learned memory store.'],
            'model': 'dashscope/deepseek-v4-flash-0731', 'base_url': 'http://47.96.153.159:8010/v1',
            'seed': 41, 'temperature': 0, 'top_p': 1, 'thinking': True,
            'reasoning_effort': 'medium', 'max_calls': 50, 'max_turns': 50,
            'max_total_tokens': 'unlimited', 'max_output_tokens': 'unlimited',
            'request_timeout': 1800, 'task_timeout': 7200, 'provider_retries': 5,
            'dataset_sha256': dataset_hashes, 'frozen_sha256': hashes,
            'tasks': tasks, 'variants': variants, 'jobs': jobs}
    write(OUT / 'plan.json', plan)
    return plan


def job_path(job):
    return OUT / 'tasks' / job['variant'] / job['task_id'].replace('/', '__')


def summarize(plan):
    report = {}
    for label in plan['variants']:
        groups = {c: {'scheduled': 20, 'terminal': 0, 'valid': 0, 'exact': 0, 'errors': {}} for c in CATS}
        for tid in plan['tasks']:
            folder = job_path({'variant': label, 'task_id': tid})
            g = groups[tid.split('/')[0]]
            if (folder / 'launch-status.json').exists(): g['terminal'] += 1
            try:
                r = json.loads((folder / 'attempt-1/results.json').read_text())[0]
            except (OSError, ValueError, IndexError): continue
            s = r.get('official_score') or {}
            if r.get('outcome_kind') in ('scored', 'scored_after_provider_failure', 'scored_after_agent_failure') and isinstance(s.get('accuracy'), (int, float)):
                g['valid'] += 1; g['exact'] += s['accuracy'] == 1
            else:
                err = r.get('error_type') or r.get('outcome_kind') or 'unknown'
                g['errors'][err] = g['errors'].get(err, 0) + 1
        report[label] = groups
    write(OUT / 'summary.json', {'variants': report, 'policy': 'One attempt per variant/task. Exact counts on fixed 20/category; pending not final. Failures and coverage reported separately.'})
    return report


def run_one(plan, job):
    folder = job_path(job)
    status = folder / 'launch-status.json'
    if status.exists(): return json.loads(status.read_text())
    attempt = folder / 'attempt-1'
    if attempt.exists():
        # Reuse only complete results; an interrupted attempt without results
        # is safe to rerun so the fixed pilot reaches full coverage.
        if (attempt / 'results.json').exists():
            return {'variant': job['variant'], 'task_id': job['task_id'], 'status': 'interrupted-needs-audit'}
        shutil.rmtree(attempt)
    folder.mkdir(parents=True, exist_ok=True)
    arm = plan['variants'][job['variant']]['arm']
    cmd = [str(PY), '-m', 'spreadsheet_harness.cli', 'benchmark', 'v2-compare',
           '--dataset', str(DATA), '--official-evaluator', str(OUT / 'frozen/evaluation.py'),
           '--skill-root', str(OUT / 'frozen/skills'), '--category', job['task_id'].split('/')[0],
           '--task-id', job['task_id'], '--arm', arm, '--output', str(attempt),
           '--composition-file', f"{arm}={OUT / 'compositions' / (job['variant'] + '.json')}",
           '--model', plan['model'], '--base-url', plan['base_url'],
           '--api-key-file', '/tmp/spreadsheet-harness-litellm.key', '--api-protocol', 'chat-completions',
           '--seed', '41', '--temperature', '0', '--top-p', '1', '--enable-thinking',
           '--reasoning-effort', 'medium', '--max-model-calls', '50', '--max-turns-per-arm', '50',
           '--max-total-tokens', 'unlimited', '--max-output-tokens', 'unlimited',
           '--task-timeout', '7200', '--request-timeout', '1800', '--litellm-timeout', '1800',
           '--request-retries', '5', '--request-interval-seconds', '1.1', '--arm-order-seed', '20260929']
    env = {k: v for k,v in os.environ.items() if not k.startswith('SHEET_HARNESS_') and k not in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy','OPENAI_API_KEY')}
    env['PYTHONPATH'] = str(OUT / 'frozen/src')
    write(folder / 'command.json', cmd)
    print(json.dumps({'event': 'started', **job}), flush=True)
    with (folder / 'launcher.log').open('a') as f:
        try: rc = subprocess.run(cmd, cwd=ROOT, env=env, stdout=f, stderr=subprocess.STDOUT, timeout=7800).returncode
        except subprocess.TimeoutExpired: rc = 124
    result = {**job, 'status': 'terminal', 'returncode': rc, 'finished_at': datetime.now(timezone.utc).isoformat()}
    write(status, result)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('mode', choices=['prepare', 'run', 'summary'])
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--plugin-only', action='store_true',
                    help='run only the four single-plugin deletion variants')
    ap.add_argument('--full', action='store_true',
                    help='run every task in all v2 categories, including Visualization')
    args = ap.parse_args()
    global OUT, CATS, FULL_RUN
    if args.full:
        OUT = FULL_OUT
        CATS = ('Template', 'Financial_Model', 'Debugging', 'Visualization')
        FULL_RUN = True
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / 'supervisor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = prepare()
        if args.mode == 'prepare':
            print(json.dumps({'variants': list(plan['variants']), 'tasks': len(plan['tasks']), 'jobs': len(plan['jobs'])})); return
        if args.mode == 'run':
            if not 1 <= args.workers <= 20: raise ValueError('workers must be 1..20')
            for name, sha in plan['frozen_sha256'].items():
                assert digest(OUT / 'frozen' / name) == sha, name
            for cat, sha in plan['dataset_sha256'].items():
                assert digest(DATA / cat / 'dataset.json') == sha, cat
            jobs = plan['jobs']
            if args.plugin_only:
                jobs = [j for j in jobs if j['variant'].startswith('minus_')]
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = [pool.submit(run_one, plan, j) for j in jobs]
                for f in as_completed(futures):
                    print(json.dumps(f.result()), flush=True)
                    summarize(plan)
        print(json.dumps(summarize(plan), indent=2))


if __name__ == '__main__': main()
