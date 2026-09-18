from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools/propose_method_candidate.py"


def _module():
    spec = importlib.util.spec_from_file_location("joint_method_proposer", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_joint_proposer_preserves_both_deterministic_coordinates(tmp_path, monkeypatch):
    module = _module()
    request = tmp_path / "request.json"
    response = tmp_path / "response.json"
    formula_path = "skills/spreadsheet-formula/SKILL.md"
    request.write_text(
        json.dumps(
            {
                "round": 1,
                "base_revision_sha256": "b" * 64,
                "route": {
                    "scope": "joint",
                    "operation": "replace",
                    "target_plugin": "profile-deterministic-compact",
                    "surface": None,
                    "mutations": [
                        {
                            "group": "harness",
                            "operation": "replace",
                            "target_plugin": "profile-deterministic-compact",
                            "surface": None,
                            "replacement_plugin": "profile-deterministic-full",
                        },
                        {
                            "group": "domain",
                            "operation": "edit",
                            "target_plugin": "skill-spreadsheet-formula",
                            "surface": "prompt",
                            "replacement_plugin": None,
                        },
                    ],
                },
                "editable_files_by_plugin": {
                    "profile-deterministic-compact": [],
                    "skill-spreadsheet-formula": [{"path": formula_path, "content": "old"}],
                },
                "operator_policy": {"scope": "joint", "routes": []},
                "evidence_packet": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        module,
        "_model_proposal",
        lambda _args, _request: {
            "rationale": "one atomic cross-group correction",
            "mutations": [
                {
                    "target_plugin": "skill-spreadsheet-formula",
                    "operation": "edit",
                    "surface": "prompt",
                    "content": "---\nname: spreadsheet-formula\n---\nnew\n",
                },
                {
                    "target_plugin": "profile-deterministic-compact",
                    "operation": "replace",
                    "surface": None,
                    "replacement_plugin": "profile-deterministic-full",
                },
            ],
        },
    )
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), str(request), str(response)])

    assert module.main() == 0
    candidate = json.loads(response.read_text(encoding="utf-8"))["candidates"][0]
    assert candidate["scope"] == "joint"
    assert [item["target_plugin"] for item in candidate["mutations"]] == [
        "profile-deterministic-compact",
        "skill-spreadsheet-formula",
    ]
    assert candidate["mutations"][0]["replacement_plugin"] == "profile-deterministic-full"
    assert candidate["mutations"][1]["operator"] == "replace-file"
    assert candidate["mutations"][1]["files"][0]["path"] == formula_path
