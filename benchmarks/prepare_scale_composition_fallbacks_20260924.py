import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
base = ROOT / 'benchmarks/results/fin15k-scale-candidates-20260923'
profile = ROOT / 'benchmarks/results/fin15k-plugin-evolution-20260920-deepseek-formal-v2-500-profiled-v7-20260922'
variants = {
    'd-only': profile / 'candidate-screen/variants/d-only/d-disable-financial',
    'joint': profile / 'candidate-screen/variants/joint/joint-profile-full',
}
for size in (50, 200):
    path = base / f'candidates-{size}.json'
    rows = json.loads(path.read_text())
    for row in rows:
        if row['mechanism'] in variants and row.get('source') != 'strict-scale-specific-glm-proposal':
            d = variants[row['mechanism']]
            rev = json.loads((d / 'revision.json').read_text())
            row['candidate_dir'] = str(d.resolve())
            row['revision_sha256'] = rev['revision_sha256']
            row['candidate_id'] = f"scale{size}-{row['mechanism']}-composition-fallback"
            row['source'] = 'scale-specific-trace-grounded-composition-fallback'
            row.pop('proposer_error', None)
            row.pop('materialization_error', None)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n')
    print(size, [(r['mechanism'], r['source']) for r in rows])
