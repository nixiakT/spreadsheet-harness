import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
src = REPO / 'benchmarks/results/fin15k-valid-common-materialized-20260929/summary.json'
out = REPO / 'benchmarks/results/fin15k-valid-common-v2-20260929'
rows = json.loads(src.read_text())
candidates = []
for row in rows:
    if row.get('status') != 'valid':
        continue
    candidates.append({
        'candidate_id': row['candidate_id'],
        'candidate_dir': row['candidate_dir'],
        'revision_sha256': row['revision_sha256'],
        'mechanism': row['mechanism'],
        'evidence_scale': row['size'],
        'source': 'fresh-valid-common-glm-candidate',
    })
out.mkdir(parents=True, exist_ok=True)
manifest = out / 'candidate-manifest.json'
manifest.write_text(json.dumps({
    'candidates': candidates,
    'policy': 'valid common Fin traces only; candidate effectiveness not yet known',
}, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'candidates': len(candidates), 'manifest': str(manifest)}))
