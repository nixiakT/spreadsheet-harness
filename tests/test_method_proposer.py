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


def test_placeholder_hunks_are_canonicalized_from_exact_source_context():
    module = _module()
    path = "src/spreadsheet_harness/example.py"
    source = "before\nvalue = 1\nmiddle\nother = 2\nafter\n"
    patch = """--- a/src/spreadsheet_harness/example.py
+++ b/src/spreadsheet_harness/example.py
@@ -X,Y +X,Y @@
 before
-value = 1
+value = 10
 middle
@@ -X,Y +X,Y @@
 middle
-other = 2
+other = 20
 after
"""

    canonical = module._canonicalize_implementation_patch(
        patch, [{"path": path, "content": source}]
    )

    assert canonical.startswith(f"diff --git a/{path} b/{path}\n")
    assert "X,Y" not in canonical
    assert "-value = 1" in canonical and "+value = 10" in canonical
    assert "-other = 2" in canonical and "+other = 20" in canonical


def test_numeric_hunk_offsets_are_rebuilt_from_source_context():
    module = _module()
    path = "src/spreadsheet_harness/example.py"
    source = "before\nvalue = 1\nmiddle\nother = 2\nafter\n"
    # The model claims this hunk starts at line 900, but the old context is
    # unique in the frozen source and should therefore still be applicable.
    patch = """diff --git a/src/spreadsheet_harness/example.py b/src/spreadsheet_harness/example.py
--- a/src/spreadsheet_harness/example.py
+++ b/src/spreadsheet_harness/example.py
@@ -900,3 +900,3 @@
 before
-value = 1
+value = 10
 middle
"""

    canonical = module._canonicalize_implementation_patch(
        patch, [{"path": path, "content": source}]
    )

    assert "@@ -1,5 +1,5 @@" in canonical
    assert "900" not in canonical
    assert "-value = 1" in canonical and "+value = 10" in canonical


def test_placeholder_hunk_rejects_ambiguous_source_context():
    module = _module()
    path = "src/spreadsheet_harness/example.py"
    patch = """--- a/src/spreadsheet_harness/example.py
+++ b/src/spreadsheet_harness/example.py
@@ -X,Y +X,Y @@
-value = 1
+value = 2
"""

    try:
        module._canonicalize_implementation_patch(
            patch,
            [{"path": path, "content": "value = 1\nvalue = 1\n"}],
        )
    except ValueError as error:
        assert "exactly one" in str(error)
    else:
        raise AssertionError("ambiguous patch context should fail closed")


def test_json_text_extracts_object_after_thinking_prose_and_fence():
    module = _module()
    parsed = module._json_text(
        "I will return the requested object now.\n```json\n"
        '{"rationale":"ok","content":"x"}\n```\n'
    )
    assert parsed == {"rationale": "ok", "content": "x"}


def test_content_uses_reasoning_content_when_final_content_is_empty():
    module = _module()
    document = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "reasoning_content": '{"rationale":"ok","content":"x"}',
                }
            }
        ]
    }
    assert module._json_text(module._content(document))["content"] == "x"
