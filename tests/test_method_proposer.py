from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools/propose_method_candidate.py"


def test_recomposition_proposer_is_deterministic_and_offline(tmp_path):
    request = tmp_path / "request.json"
    response = tmp_path / "response.json"
    request.write_text(
        json.dumps(
            {
                "round": 2,
                "base_revision_sha256": "a" * 64,
                "route": {
                    "operation": "replace",
                    "target_plugin": "profile-deterministic-compact",
                    "replacement_plugin": "profile-deterministic-full",
                    "surface": None,
                },
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [str(ROOT / ".venv/bin/python"), str(SCRIPT), str(request), str(response)],
        check=True,
        cwd=ROOT,
    )
    candidate = json.loads(response.read_text())["candidates"][0]
    assert candidate["operation"] == "replace"
    assert candidate["replacement_plugin"] == "profile-deterministic-full"
    assert "patch" not in candidate and "files" not in candidate
