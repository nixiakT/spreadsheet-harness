from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from openpyxl import load_workbook
from PIL import Image

from spreadsheet_harness.evidence_contract import (
    PIXEL_SHA256_ALGORITHM,
    ContractMode,
    ContractSpec,
    ContractStateError,
    EventKind,
    EvidenceEvent,
    EvidenceScope,
    audit_evidence_certificate,
)
from spreadsheet_harness.session import WorkbookSession
from spreadsheet_harness.tools import SpreadsheetToolRegistry, ToolOutcome

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_CONTRACT = ROOT / "contracts" / "spreadsheet-evidence-v1.yaml"


def _workbook_effects(**updates: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": "workbook-effect-diff-v1",
        "semantic_changed": True,
        "complete": True,
        "effects": ["value"],
        "scope": {
            "wildcard": False,
            "sheets": [],
            "ranges": [{"sheet": "Sales", "range": "B4:B4", "cell_count": 1}],
        },
        "formula_scope": {"wildcard": False, "sheets": [], "ranges": []},
        "changed_cell_count": 1,
        "scanned_cell_count": 20,
        "reasons": [],
    }
    value.update(updates)
    return value


def _grounded_declaration(
    tools: SpreadsheetToolRegistry,
    *,
    observed_range: str,
    target_range: str | None = None,
) -> tuple[int, int]:
    inspection = tools.invoke(
        "inspect_range",
        {"sheet": "Sales", "range_ref": observed_range},
    ).data
    assert inspection["ok"] is True
    observation_id = inspection["observation_id"]
    declaration = tools.invoke(
        "declare_edit_target",
        {
            "targets": [{"sheet": "Sales", "range_ref": target_range or observed_range}],
            "observation_ids": [observation_id],
        },
    ).data
    assert declaration["ok"] is True
    return observation_id, declaration["declaration_id"]


def _install_fake_render(
    tools: SpreadsheetToolRegistry,
    *,
    render_id: str = "render-current",
    mode: str = "per_sheet",
    pages: list[dict[str, Any]] | None = None,
) -> list[Path]:
    session = tools.session
    render_dir = session.paths.artifacts / "render" / render_id
    render_dir.mkdir(parents=True, exist_ok=True)
    specifications = pages or [
        {
            "filename": "page-1.png",
            "sheet": "Sales",
            "sheet_page": 1,
            "size": (2, 2),
            "color": "white",
        }
    ]
    manifest_pages: list[dict[str, Any]] = []
    paths: list[Path] = []
    for index, specification in enumerate(specifications, start=1):
        path = render_dir / str(specification["filename"])
        Image.new(
            str(specification.get("image_mode", "RGB")),
            tuple(specification.get("size", (2, 2))),
            specification.get("color", "white"),
        ).save(path)
        paths.append(path.resolve())
        data = path.read_bytes()
        width, height = tuple(specification.get("size", (2, 2)))
        sheet = specification.get("sheet")
        sheet_page = specification.get("sheet_page")
        page: dict[str, Any] = {
            "index": index,
            "page": sheet_page if sheet_page is not None else index,
            "path": path.name,
            "image_path": str(path.resolve()),
            "sha256": hashlib.sha256(data).hexdigest(),
            "width": width,
            "height": height,
            "sheet": sheet,
            "sheet_page": sheet_page,
        }
        if "cell_scope" in specification:
            scope = specification["cell_scope"]
            page["cell_scope"] = (
                scope.to_dict() if isinstance(scope, EvidenceScope) else scope
            )
        manifest_pages.append(page)
    artifact = session.artifact_ref()
    manifest_path = render_dir / "render-manifest.json"
    manifest = {
        "schema_version": 1,
        "source": {
            "name": session.workbook_path.name,
            "format": session.workbook_path.suffix.lower().lstrip("."),
            "sha256": artifact.sha256,
        },
        "backend": "libreoffice-pymupdf",
        "version": {"libreoffice": "test", "pymupdf": "test"},
        "hash": artifact.sha256,
        "mode": mode,
        "dpi": 144,
        "manifest_path": str(manifest_path.resolve()),
        "page_count": len(manifest_pages),
        "pages": manifest_pages,
    }
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    tools._last_render = {
        **manifest,
        "render_id": render_id,
        "render_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "artifact_revision": artifact.revision,
        "artifact_sha256": artifact.sha256,
    }
    return paths


def _observe_fake_render(tools: SpreadsheetToolRegistry) -> dict[str, Any]:
    render = tools._validated_current_render()
    monitor = tools.evidence_monitor
    assert monitor is not None
    monitor.observe(
        EvidenceEvent(
            EventKind.WORKBOOK_RENDERED,
            tools.session.artifact_ref().sha256,
            render_id=render["render_id"],
            render_manifest_sha256=render["render_manifest_sha256"],
            metadata=tools._portable_render_metadata(render),
        )
    )
    return render


def _visual_only_contract() -> ContractSpec:
    return ContractSpec.from_mapping(
        {
            "schema_version": 1,
            "rules": [
                {
                    "id": "visual",
                    "trigger": "effects.visual_changed",
                    "require_sequence": [
                        {"event": "workbook.rendered", "artifact": "current"},
                        {
                            "event": "rendered_page.viewed",
                            "artifact": "same_render",
                            "scope": "changed_visual_scope",
                        },
                    ],
                }
            ],
        }
    )


def test_tool_registry_dispatch_and_errors(sample_workbook: Path, tmp_path: Path) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(session, enable_code=False)
    names = {item["name"] for item in tools.schemas}
    assert {"list_sheets", "inspect_range", "range_to_latex", "view_image"} <= names
    assert "code_interpreter" not in names
    assert tools.invoke("list_sheets", {}).data["ok"] is True
    result = tools.invoke("inspect_range", {"sheet": "Nope", "range_ref": "A1"})
    assert result.data["ok"] is False
    assert result.data["type"] == "ToolInputError"


def test_target_grounding_feature_is_off_by_default_and_preserves_tool_contract(
    sample_workbook: Path, tmp_path: Path
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(session, enable_code=False)
    schemas = {item["name"]: item for item in tools.schemas}

    assert "declare_edit_target" not in schemas
    assert "declaration_id" not in schemas["write_range"]["parameters"]["properties"]
    result = tools.invoke(
        "write_range",
        {"sheet": "Sales", "start_cell": "B4", "values": [[7]]},
    ).data

    assert result["ok"] is True
    assert "target_grounding" not in result


def test_grounded_target_schema_and_handler_require_bounded_ranges(
    sample_workbook: Path,
    tmp_path: Path,
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(
        session,
        enable_code=False,
        enable_target_grounding=True,
    )
    declaration_schema = next(
        schema for schema in tools.schemas if schema["name"] == "declare_edit_target"
    )
    target_schema = declaration_schema["parameters"]["properties"]["targets"]["items"]
    assert target_schema["required"] == ["sheet", "range_ref"]
    assert target_schema["properties"]["range_ref"]["type"] == "string"

    inspection = tools.invoke(
        "inspect_range",
        {"sheet": "Sales", "range_ref": "B4"},
    ).data
    for target in ({"sheet": "Sales"}, {"sheet": "Sales", "range_ref": None}):
        rejected = tools.invoke(
            "declare_edit_target",
            {
                "targets": [target],
                "observation_ids": [inspection["observation_id"]],
            },
        ).data
        assert rejected["ok"] is False
        assert rejected["type"] == "ToolInputError"


def test_grounded_native_mutation_publishes_exact_transition_and_provenance(
    sample_workbook: Path, tmp_path: Path
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(
        session,
        enable_code=False,
        enable_target_grounding=True,
    )
    _, declaration_id = _grounded_declaration(
        tools,
        observed_range="B4:C4",
        target_range="B4:C4",
    )

    result = tools.invoke(
        "write_range",
        {
            "sheet": "Sales",
            "start_cell": "B4",
            "values": [[7, 8]],
            "declaration_id": declaration_id,
        },
    ).data

    assert result["ok"] is True
    assert result["target_grounding"]["decision"] == "authorized"
    transition = session.artifact_transitions[-1]
    assert result["artifact_transition_id"] == transition.transition_id
    assert result["workbook_sha256_before"] == transition.before.sha256
    assert result["workbook_sha256_after"] == transition.after.sha256
    trajectory = [json.loads(line) for line in session.paths.trajectory.read_text().splitlines()]
    returned = [row for row in trajectory if row["event"] == "tool.returned"][-1]
    assert returned["payload"]["result"]["target_grounding"] == result["target_grounding"]

    replay = tools.invoke(
        "write_range",
        {
            "sheet": "Sales",
            "start_cell": "B4",
            "values": [[9]],
            "declaration_id": declaration_id,
        },
    ).data
    assert replay["ok"] is False
    assert replay["type"] == "TargetGroundingError"


def test_uninspected_expansion_and_out_of_scope_native_diff_are_rejected_atomically(
    sample_workbook: Path, tmp_path: Path
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(
        session,
        enable_code=False,
        enable_target_grounding=True,
    )
    observation_id, declaration_id = _grounded_declaration(
        tools,
        observed_range="B4",
        target_range="B4",
    )
    before_bytes = session.workbook_path.read_bytes()
    before_artifact = session.artifact_ref()
    before_transitions = session.artifact_transitions

    expansion = tools.invoke(
        "declare_edit_target",
        {
            "targets": [{"sheet": "Sales", "range_ref": "B4:C4"}],
            "observation_ids": [observation_id],
        },
    ).data
    assert expansion["ok"] is False
    assert expansion["type"] == "TargetGroundingError"

    rejected = tools.invoke(
        "write_range",
        {
            "sheet": "Sales",
            "start_cell": "B4",
            "values": [[7, 8]],
            "declaration_id": declaration_id,
        },
    ).data

    assert rejected["ok"] is False
    assert rejected["type"] == "TargetGroundingRejected"
    assert rejected["target_grounding"]["decision"] == "rejected.outside_declared_target"
    assert session.workbook_path.read_bytes() == before_bytes
    assert session.artifact_ref() == before_artifact
    assert session.artifact_transitions == before_transitions


def test_grounded_semantic_noop_is_not_recorded_as_a_user_edit(
    sample_workbook: Path, tmp_path: Path
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(
        session,
        enable_code=False,
        enable_target_grounding=True,
    )
    inspection = tools.invoke("inspect_range", {"sheet": "Sales", "range_ref": "B2"}).data
    _, declaration_id = _grounded_declaration(
        tools,
        observed_range="B2",
        target_range="B2",
    )

    result = tools.invoke(
        "write_range",
        {
            "sheet": "Sales",
            "start_cell": "B2",
            "values": [[inspection["matrix"][0][0]]],
            "declaration_id": declaration_id,
        },
    ).data

    assert result["ok"] is True
    assert result["workbook_effects"]["semantic_changed"] is False
    assert result["target_grounding"]["decision"] == "authorized_no_op"


def test_grounded_guard_rejection_does_not_create_evidence_rollback_blocker(
    sample_workbook: Path, tmp_path: Path
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(
        session,
        enable_code=False,
        enable_target_grounding=True,
        evidence_contract=ContractSpec.load(EVIDENCE_CONTRACT),
        contract_mode=ContractMode.ENFORCE,
    )
    _, declaration_id = _grounded_declaration(
        tools,
        observed_range="B4",
        target_range="B4",
    )

    rejected = tools.invoke(
        "write_range",
        {
            "sheet": "Sales",
            "start_cell": "B4",
            "values": [[7, 8]],
            "declaration_id": declaration_id,
        },
    ).data

    assert rejected["ok"] is False
    assert rejected["workbook_rolled_back"] is True
    assert rejected["_evidence_contract"]["pending_count"] == 0
    assert tools.evidence_monitor is not None
    status = tools.evidence_monitor.status()
    assert status["unresolved_failures"] == []
    assert status["pending_obligations"] == []


def test_grounded_opaque_code_publishes_only_an_in_scope_staged_diff(
    sample_workbook: Path, tmp_path: Path
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(session, enable_target_grounding=True)
    _, declaration_id = _grounded_declaration(
        tools,
        observed_range="B4",
        target_range="B4",
    )

    result = tools.invoke(
        "code_interpreter",
        {
            "code": """wb = sheet_harness.load_workbook()
wb["Sales"]["B4"] = 17
sheet_harness.save_workbook(wb)
wb.close()
print("staged edit complete")
""",
            "declaration_id": declaration_id,
        },
    ).data

    assert result["ok"] is True
    assert result["target_grounding"]["decision"] == "authorized"
    assert result["workbook_effects"]["scope"]["ranges"][0]["range"] == "B4:B4"
    assert session.inspect_range("Sales", "B4")["matrix"] == [[17]]


def test_grounded_opaque_code_rejection_restores_bytes_and_never_publishes(
    sample_workbook: Path, tmp_path: Path
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(session, enable_target_grounding=True)
    _, declaration_id = _grounded_declaration(
        tools,
        observed_range="B4",
        target_range="B4",
    )
    before_bytes = session.workbook_path.read_bytes()
    before_artifact = session.artifact_ref()

    result = tools.invoke(
        "code_interpreter",
        {
            "code": """wb = sheet_harness.load_workbook()
wb["Sales"]["C4"] = 99
sheet_harness.save_workbook(wb)
wb.close()
""",
            "declaration_id": declaration_id,
        },
    ).data

    assert result["ok"] is False
    assert result["type"] == "TargetGroundingRejected"
    assert result["target_grounding"]["decision"] == "rejected.outside_declared_target"
    assert result["artifact_transition_id"] is None
    assert session.workbook_path.read_bytes() == before_bytes
    assert session.artifact_ref() == before_artifact
    assert session.artifact_transitions == ()


def test_grounded_code_workspace_does_not_expose_session_ledger_by_default(
    sample_workbook: Path, tmp_path: Path
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(session, enable_target_grounding=True)
    _, declaration_id = _grounded_declaration(
        tools,
        observed_range="B4",
        target_range="B4",
    )

    result = tools.invoke(
        "code_interpreter",
        {
            "code": """from pathlib import Path
names = sorted(path.name for path in Path.cwd().iterdir())
assert "trajectory.jsonl" not in names
assert not (Path.cwd() / "target_grounding.json").exists()
print(names)
""",
            "declaration_id": declaration_id,
        },
    ).data

    assert result["ok"] is True
    assert "trajectory.jsonl" not in result["stdout"]
    assert result["target_grounding"]["decision"] == "authorized_no_op"


def test_recalculation_transition_stales_grounding_observation_and_declaration(
    sample_workbook: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(
        session,
        enable_code=False,
        enable_target_grounding=True,
    )
    observation_id, declaration_id = _grounded_declaration(
        tools,
        observed_range="B4",
        target_range="B4",
    )

    def fake_recalculate(
        source: Path,
        destination: Path,
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        assert timeout_seconds == 120.0
        source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        temporary = destination.with_name("fake-recalculated.xlsx")
        workbook = load_workbook(source)
        workbook["Sales"]["A10"] = "derived recalculation marker"
        workbook.save(temporary)
        workbook.close()
        temporary.replace(destination)
        return {
            "backend": "test-recalculator",
            "version": "1",
            "source_sha256": source_sha256,
            "output_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            "atomic_replace": True,
        }

    monkeypatch.setattr(
        "spreadsheet_harness.render.recalculate_workbook",
        fake_recalculate,
    )
    recalculated = tools.invoke(
        "recalculate_and_read",
        {"sheet": "Sales", "range_ref": "A10"},
    ).data
    assert recalculated["ok"] is True
    assert session.artifact_ref().revision == 1

    stale_observation = tools.invoke(
        "declare_edit_target",
        {
            "targets": [{"sheet": "Sales", "range_ref": "B4"}],
            "observation_ids": [observation_id],
        },
    ).data
    assert stale_observation["ok"] is False
    assert stale_observation["type"] == "TargetGroundingError"

    stale_declaration = tools.invoke(
        "write_range",
        {
            "sheet": "Sales",
            "start_cell": "B4",
            "values": [[7]],
            "declaration_id": declaration_id,
        },
    ).data
    assert stale_declaration["ok"] is False
    assert stale_declaration["type"] == "TargetGroundingError"


def test_grounded_runtime_rejects_unknown_harness_diff_before_publication(
    sample_workbook: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spreadsheet_harness.workbook_diff import WorkbookEffectDiff

    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(
        session,
        enable_code=False,
        enable_target_grounding=True,
    )
    _, declaration_id = _grounded_declaration(
        tools,
        observed_range="B4",
        target_range="B4",
    )
    before_bytes = session.workbook_path.read_bytes()
    before_artifact = session.artifact_ref()
    monkeypatch.setattr(
        "spreadsheet_harness.session.diff_workbooks",
        lambda *_args, **_kwargs: WorkbookEffectDiff.unknown("forced opaque diff"),
    )

    result = tools.invoke(
        "write_range",
        {
            "sheet": "Sales",
            "start_cell": "B4",
            "values": [[7]],
            "declaration_id": declaration_id,
        },
    ).data

    assert result["ok"] is False
    assert result["target_grounding"]["decision"] == "rejected.unknown_effect"
    assert session.workbook_path.read_bytes() == before_bytes
    assert session.artifact_ref() == before_artifact
    assert session.artifact_transitions == ()


def test_registry_shadow_monitor_binds_mutation_and_inspection_to_revision(
    sample_workbook: Path, tmp_path: Path
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(
        session,
        enable_code=False,
        allowed_tools={"write_range", "inspect_range"},
        evidence_contract=ContractSpec.load(EVIDENCE_CONTRACT),
        contract_mode=ContractMode.SHADOW,
    )

    changed = tools.invoke(
        "write_range",
        {"sheet": "Sales", "start_cell": "B4", "values": [[7]]},
    )
    changed_status = changed.data["_evidence_contract"]
    assert changed_status["submission_ready"] is False
    assert changed_status["next_required_event"] == "range.inspected"
    assert changed_status["pending_count"] == 1

    inspected = tools.invoke(
        "inspect_range",
        {"sheet": "Sales", "range_ref": "A3:C5"},
    )
    inspected_status = inspected.data["_evidence_contract"]
    assert inspected.data["artifact_revision"] == 1
    assert inspected_status["submission_ready"] is True
    assert inspected_status["pending_count"] == 0
    assert tools.evidence_monitor is not None
    certificate = tools.evidence_monitor.certificate()
    assert [item["kind"] for item in certificate["events"]] == [
        "mutation.committed",
        "range.inspected",
    ]


def test_registry_does_not_treat_semantic_resave_as_user_edit(
    sample_workbook: Path, tmp_path: Path
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    existing = session.inspect_range("Sales", "A2:A2")["matrix"][0][0]
    tools = SpreadsheetToolRegistry(
        session,
        enable_code=False,
        allowed_tools={"write_range"},
        evidence_contract=ContractSpec.load(EVIDENCE_CONTRACT),
        contract_mode=ContractMode.SHADOW,
    )

    result = tools.invoke(
        "write_range",
        {"sheet": "Sales", "start_cell": "A2", "values": [[existing]]},
    )

    assert result.data["ok"] is True
    assert result.data["workbook_effects"]["semantic_changed"] is False
    assert result.data["_evidence_contract"]["artifact_changed"] is False
    assert result.data["_evidence_contract"]["submission_ready"] is False


def test_registry_rejects_changed_artifact_without_trusted_footprint(
    sample_workbook: Path, tmp_path: Path
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(
        session,
        enable_code=False,
        allowed_tools={"write_range"},
        evidence_contract=ContractSpec.load(EVIDENCE_CONTRACT),
        contract_mode=ContractMode.ENFORCE,
    )
    write_range = tools._handlers["write_range"]

    def omit_footprint(arguments: dict[str, Any]) -> ToolOutcome:
        outcome = write_range(arguments)
        outcome.data.pop("workbook_effects")
        return outcome

    tools._handlers["write_range"] = omit_footprint

    with pytest.raises(
        ContractStateError,
        match="changed workbook bytes without a trusted workbook_effects footprint",
    ):
        tools.invoke(
            "write_range",
            {"sheet": "Sales", "start_cell": "B4", "values": [[7]]},
        )


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"changed_cell_count": True}, "counts must be non-negative integers"),
        ({"scanned_cell_count": -1}, "counts must be non-negative integers"),
        (
            {"changed_cell_count": 21, "scanned_cell_count": 20},
            "changed_cell_count cannot exceed scanned_cell_count",
        ),
        (
            {"changed_cell_count": 0},
            "cell effects require a positive changed_cell_count",
        ),
        ({"reasons": "not-a-list"}, "reasons must be a string list"),
        ({"reasons": ["ok", 7]}, "reasons must be a string list"),
        ({"effects": ["value", "value"]}, "effects must not contain duplicates"),
        (
            {
                "semantic_changed": False,
                "effects": [],
                "scope": {"wildcard": False, "sheets": [], "ranges": []},
                "formula_scope": {
                    "wildcard": False,
                    "sheets": [],
                    "ranges": [{"sheet": "Sales", "range": "B4:B4", "cell_count": 1}],
                },
                "changed_cell_count": 0,
            },
            "no-op must not report",
        ),
        (
            {"effects": ["formula"]},
            "formula effect requires a non-empty formula_scope",
        ),
        (
            {
                "effects": ["formula"],
                "formula_scope": {
                    "wildcard": False,
                    "sheets": [],
                    "ranges": [{"sheet": "Sales", "range": "D8:D8", "cell_count": 1}],
                },
            },
            "scope must cover formula_scope",
        ),
        (
            {"effects": ["unknown"]},
            "complete workbook effect diff must not report unknown",
        ),
        (
            {"complete": False},
            "incomplete diff must be fail-closed",
        ),
    ],
)
def test_trusted_workbook_effects_reject_malformed_footprints(
    updates: dict[str, Any], message: str
) -> None:
    with pytest.raises(ContractStateError, match=message):
        SpreadsheetToolRegistry._trusted_workbook_effects(
            {"workbook_effects": _workbook_effects(**updates)}
        )


def test_recalculation_certificate_uses_only_portable_bound_metadata(
    sample_workbook: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(
        session,
        enable_code=False,
        allowed_tools={"write_range", "recalculate_and_read"},
        evidence_contract=ContractSpec.load(EVIDENCE_CONTRACT),
        contract_mode=ContractMode.ENFORCE,
    )
    changed = tools.invoke(
        "write_range",
        {"sheet": "Sales", "start_cell": "B4", "values": [["=1+1"]]},
    )
    assert changed.data["ok"] is True
    artifact = session.artifact_ref()

    def fake_recalculate() -> dict[str, Any]:
        return {
            "backend": "libreoffice-headless",
            "version": "LibreOffice 25.2.4",
            "profile": "isolated-per-invocation",
            "source_path": "/private/session/input.xlsx",
            "destination_path": "/private/session/output.xlsx",
            "source_sha256": artifact.sha256,
            "output_sha256": artifact.sha256,
            "format": "xlsx",
            "atomic_replace": True,
        }

    monkeypatch.setattr(session, "recalculate", fake_recalculate)
    result = tools.invoke(
        "recalculate_and_read",
        {"sheet": "Sales", "range_ref": "A3:C5"},
    )
    assert result.data["ok"] is True
    assert tools.evidence_monitor is not None
    certificate = tools.evidence_monitor.certificate()
    calculation = next(
        event["metadata"]["calculation"]
        for event in certificate["events"]
        if event["kind"] == "workbook.recalculated"
    )
    assert calculation == {
        "backend": "libreoffice-headless",
        "version": "LibreOffice 25.2.4",
        "source_sha256": artifact.sha256,
        "output_sha256": artifact.sha256,
        "atomic_replace": True,
    }
    assert "/private/session" not in json.dumps(certificate)
    assert audit_evidence_certificate(certificate)["valid"] is True


def test_recalculation_metadata_must_bind_source_and_output_artifacts(
    sample_workbook: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(
        session,
        enable_code=False,
        allowed_tools={"recalculate_and_read"},
        evidence_contract=ContractSpec.load(EVIDENCE_CONTRACT),
    )
    artifact = session.artifact_ref()

    monkeypatch.setattr(
        session,
        "recalculate",
        lambda: {
            "backend": "libreoffice-headless",
            "version": "LibreOffice 25.2.4",
            "source_sha256": "f" * 64,
            "output_sha256": artifact.sha256,
            "atomic_replace": True,
        },
    )

    with pytest.raises(ContractStateError, match="source_sha256"):
        tools.invoke(
            "recalculate_and_read",
            {"sheet": "Sales", "range_ref": "A1:B2"},
        )


def test_code_interpreter_effect_scope_is_computed_from_workbook_diff(
    sample_workbook: Path, tmp_path: Path
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(
        session,
        allowed_tools={"code_interpreter", "inspect_range"},
        evidence_contract=ContractSpec.load(EVIDENCE_CONTRACT),
        contract_mode=ContractMode.SHADOW,
    )

    edited = tools.invoke(
        "code_interpreter",
        {
            "code": """wb = sheet_harness.load_workbook()
ws = wb[\"Sales\"]
ws[\"B4\"] = 9
sheet_harness.save_workbook(wb)
wb.close()
"""
        },
    )

    assert edited.data["ok"] is True
    assert edited.data["workbook_effects"]["effects"] == ["value"]
    assert edited.data["workbook_effects"]["scope"]["ranges"][0]["range"] == "B4:B4"
    assert edited.data["_evidence_contract"]["pending_count"] == 1
    assert edited.data["_evidence_contract"]["next_required_event"] == "range.inspected"

    inspected = tools.invoke(
        "inspect_range",
        {"sheet": "Sales", "range_ref": "A3:C5"},
    )
    assert inspected.data["_evidence_contract"]["submission_ready"] is True


def test_code_interpreter_schema_requires_self_contained_calls(
    sample_workbook: Path, tmp_path: Path
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(session, enable_code=True)
    schema = next(item for item in tools.schemas if item["name"] == "code_interpreter")
    description = schema["description"]

    assert "fresh Python process" in description
    assert "variables, imports, and workbook objects do not persist" in description
    assert "editing or recovery script self-contained" in description
    workflow = description.split("self-contained:", 1)[1]
    expected_steps = (
        "import",
        "load",
        "re-read the request and inspected workbook state",
        "edit",
        "save",
        "close",
        "reopen",
        "verify the requested change and nearby cells",
        "print compact verification",
    )
    positions = [workflow.index(step) for step in expected_steps]
    assert positions == sorted(positions)


def test_tool_registry_redacts_configured_secret_from_recorded_outcome(
    sample_workbook: Path, tmp_path: Path
) -> None:
    unusual_secret = "credential-with-an-unusual-shape"
    session = WorkbookSession.create(
        sample_workbook,
        tmp_path / "run",
        recorder_secrets=(unusual_secret,),
    )
    tools = SpreadsheetToolRegistry(
        session,
        enable_code=True,
        allowed_tools={"code_interpreter"},
    )

    class LeakingInterpreter:
        def run(self, *_: Any, **__: Any) -> dict[str, Any]:
            return {
                "ok": False,
                "stdout": unusual_secret,
                "nested": {"message": unusual_secret},
            }

    tools.interpreter = LeakingInterpreter()  # type: ignore[assignment]
    result = tools.invoke("code_interpreter", {"code": "print('x')"})

    assert result.data["stdout"] == unusual_secret
    assert unusual_secret not in session.paths.trajectory.read_text(encoding="utf-8")


def test_allowed_tools_filters_schemas_and_dispatch(sample_workbook: Path, tmp_path: Path) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    allowed = {"list_sheets", "range_to_latex"}
    tools = SpreadsheetToolRegistry(session, enable_code=False, allowed_tools=allowed)
    allowed.clear()

    assert {item["name"] for item in tools.schemas} == {"list_sheets", "range_to_latex"}
    assert tools.invoke("list_sheets", {}).data["ok"] is True

    before = session.workbook_path.read_bytes()
    blocked = tools.invoke(
        "write_range", {"sheet": "Sales", "start_cell": "A1", "values": [["changed"]]}
    )
    assert blocked.data == {
        "ok": False,
        "error": "Unknown tool: write_range",
        "type": "UnknownTool",
    }
    assert session.workbook_path.read_bytes() == before


def test_view_image_only_accepts_page_from_most_recent_render(
    sample_workbook: Path, tmp_path: Path
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(
        session,
        enable_code=False,
        allowed_tools={"render_workbook", "view_image"},
    )
    render_root = session.paths.artifacts / "render"
    stale = render_root / "render-old" / "page.png"
    stale.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (2, 2), "white").save(stale)

    before_render = tools.invoke("view_image", {"image_path": str(stale)}).data
    assert before_render["ok"] is False
    assert "render_workbook before" in before_render["error"]

    latest = _install_fake_render(tools, render_id="render-new")[0]
    stale_result = tools.invoke("view_image", {"image_path": str(stale)}).data
    assert stale_result["ok"] is False
    assert "most recent render_workbook" in stale_result["error"]

    latest_result = tools.invoke("view_image", {"image_path": str(latest)})
    assert latest_result.data["ok"] is True
    assert latest_result.image_path == latest.resolve()
    assert latest_result.data["visual_evidence_status"] == "pending_provider_response"
    assert latest_result.data["page_file_sha256"] == latest_result.data["page_sha256"]
    assert latest_result.data["pixel_sha256_algorithm"] == PIXEL_SHA256_ALGORITHM


def test_view_image_rejects_latest_render_after_workbook_revision_changes(
    sample_workbook: Path, tmp_path: Path
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(
        session,
        enable_code=False,
        allowed_tools={"write_range", "view_image"},
    )
    page = _install_fake_render(tools)[0]

    changed = tools.invoke(
        "write_range",
        {"sheet": "Sales", "start_cell": "A2", "values": [["changed"]]},
    )
    assert changed.data["ok"] is True

    viewed = tools.invoke("view_image", {"image_path": str(page)})
    assert viewed.data["ok"] is False
    assert "render is stale" in viewed.data["error"]


def test_view_image_revalidates_manifest_page_bytes_and_decoded_dimensions(
    sample_workbook: Path,
    tmp_path: Path,
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(
        session,
        enable_code=False,
        allowed_tools={"view_image"},
    )
    page = _install_fake_render(tools)[0]
    manifest_path = Path(str(tools._last_render["manifest_path"]))  # type: ignore[index]

    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8") + " ",
        encoding="utf-8",
    )
    with pytest.raises(ContractStateError, match="manifest hash"):
        tools.invoke("view_image", {"image_path": str(page)})

    page = _install_fake_render(tools, render_id="render-second")[0]
    Image.new("RGB", (2, 2), "red").save(page)
    with pytest.raises(ContractStateError, match="page bytes"):
        tools.invoke("view_image", {"image_path": str(page)})

    page = _install_fake_render(tools, render_id="render-third")[0]
    assert tools._last_render is not None
    manifest_path = Path(str(tools._last_render["manifest_path"]))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pages"][0]["width"] = 3
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    tools._last_render["pages"] = manifest["pages"]
    tools._last_render["render_manifest_sha256"] = hashlib.sha256(
        manifest_path.read_bytes()
    ).hexdigest()
    with pytest.raises(ContractStateError, match="dimensions"):
        tools.invoke("view_image", {"image_path": str(page)})


def test_visual_evidence_is_two_phase_and_requires_all_affected_sheet_pages(
    sample_workbook: Path,
    tmp_path: Path,
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(
        session,
        enable_code=False,
        allowed_tools={"format_range", "view_image"},
        evidence_contract=_visual_only_contract(),
        contract_mode=ContractMode.ENFORCE,
    )
    mutation = tools.invoke(
        "format_range",
        {
            "sheet": "Sales",
            "range_ref": "A2:A2",
            "format_spec": {"font": {"bold": True}},
        },
    )
    assert mutation.data["ok"] is True
    pages = _install_fake_render(
        tools,
        pages=[
            {
                "filename": "page-1.png",
                "sheet": "Sales",
                "sheet_page": 1,
                "size": (2, 2),
                "color": "white",
            },
            {
                "filename": "page-2.png",
                "sheet": "Sales",
                "sheet_page": 2,
                "size": (2, 2),
                "color": "blue",
            },
        ],
    )
    _observe_fake_render(tools)
    monitor = tools.evidence_monitor
    assert monitor is not None
    assert monitor.status()["event_count"] == 2

    first = tools.invoke("view_image", {"image_path": str(pages[0])})
    first_id = first.data["visual_confirmation_id"]
    assert monitor.status()["event_count"] == 2
    assert monitor.submission_decision().allowed is False
    first_status = tools.confirm_view_image_delivery(
        first_id,
        attached_file_sha256=first.data["page_file_sha256"],
        provider_response_id="response-after-first-page",
    )
    assert first_status["submission_ready"] is False
    assert monitor.status()["event_count"] == 3
    obligation = monitor.obligations[0]
    assert obligation.required_page_ids == ("Sales:1", "Sales:2")
    assert obligation.viewed_page_ids == ("Sales:1",)

    second = tools.invoke("view_image", {"image_path": str(pages[1])})
    assert monitor.status()["event_count"] == 3
    second_status = tools.confirm_view_image_delivery(
        second.data["visual_confirmation_id"],
        attached_file_sha256=second.data["page_file_sha256"],
        provider_response_id="response-after-second-page",
    )
    assert second_status["submission_ready"] is True
    assert monitor.submission_decision().allowed is True
    assert len(obligation.witnesses) == 3
    page_witnesses = [
        witness
        for witness in obligation.witnesses
        if witness.kind is EventKind.RENDERED_PAGE_VIEWED
    ]
    assert len(page_witnesses) == 2
    assert all(witness.page_file_sha256 for witness in page_witnesses)
    assert all(witness.page_pixel_sha256 for witness in page_witnesses)
    assert all(
        witness.pixel_sha256_algorithm == PIXEL_SHA256_ALGORITHM
        for witness in page_witnesses
    )


def test_visual_confirmation_is_one_shot_and_rechecks_attachment_and_page(
    sample_workbook: Path,
    tmp_path: Path,
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(
        session,
        enable_code=False,
        allowed_tools={"view_image"},
    )
    page = _install_fake_render(tools)[0]
    staged = tools.invoke("view_image", {"image_path": str(page)}).data
    confirmation_id = staged["visual_confirmation_id"]

    with pytest.raises(ContractStateError, match="Attached image bytes"):
        tools.confirm_view_image_delivery(
            confirmation_id,
            attached_file_sha256="f" * 64,
            provider_response_id="response-1",
        )
    with pytest.raises(ContractStateError, match="unknown, expired, or already used"):
        tools.confirm_view_image_delivery(
            confirmation_id,
            attached_file_sha256=staged["page_file_sha256"],
            provider_response_id="response-1",
        )

    staged = tools.invoke("view_image", {"image_path": str(page)}).data
    confirmation_id = staged["visual_confirmation_id"]
    with pytest.raises(ContractStateError, match="provider response ID"):
        tools.confirm_view_image_delivery(
            confirmation_id,
            attached_file_sha256=staged["page_file_sha256"],
            provider_response_id="",
        )

    staged = tools.invoke("view_image", {"image_path": str(page)}).data
    confirmation_id = staged["visual_confirmation_id"]
    Image.new("RGB", (2, 2), "red").save(page)
    with pytest.raises(ContractStateError, match="page bytes"):
        tools.confirm_view_image_delivery(
            confirmation_id,
            attached_file_sha256=staged["page_file_sha256"],
            provider_response_id="response-2",
        )
    with pytest.raises(ContractStateError, match="unknown, expired, or already used"):
        tools.confirm_view_image_delivery(
            confirmation_id,
            attached_file_sha256=staged["page_file_sha256"],
            provider_response_id="response-2",
        )


def test_range_to_latex_escapes_values_and_reports_structure(
    sample_workbook: Path, tmp_path: Path
) -> None:
    workbook = load_workbook(sample_workbook)
    workbook["Sales"]["A2"] = "unsafe &_%$#{}~^\\\nnext"
    workbook.save(sample_workbook)
    workbook.close()

    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(session, enable_code=False, allowed_tools={"range_to_latex"})
    before = session.workbook_path.read_bytes()
    result = tools.invoke("range_to_latex", {"sheet": "Sales", "range_ref": "A1:D5"}).data

    assert result["ok"] is True
    assert (result["rows"], result["columns"], result["cell_count"]) == (5, 4, 20)
    assert result["latex"].startswith(r"\begin{tabular}{llll}")
    assert result["latex"].endswith(r"\end{tabular}")
    assert r"unsafe \&\_\%\$\#\{\}\textasciitilde{}\textasciicircum{}" in result["latex"]
    assert r"\textbackslash{}\newline{}next" in result["latex"]
    assert result["merged_ranges"] == ["A5:B5"]
    heading_style = next(
        style for style in result["style_summary"]["styles"] if "A1" in style["sample_cells"]
    )
    assert heading_style["font"]["bold"] is True
    assert heading_style["fill"] == "FF336699"
    assert session.workbook_path.read_bytes() == before


def test_range_to_latex_enforces_cell_and_output_limits(
    sample_workbook: Path, tmp_path: Path
) -> None:
    workbook = load_workbook(sample_workbook)
    workbook["Sales"]["A2"] = "\\" * 30_000
    workbook.save(sample_workbook)
    workbook.close()

    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(session, enable_code=False)
    bounded = tools.invoke("range_to_latex", {"sheet": "Sales", "range_ref": "A2"}).data

    assert bounded["ok"] is True
    assert bounded["latex_truncated"] is True
    assert bounded["truncated_cell_count"] == 1
    assert len(bounded["latex"]) <= bounded["limits"]["max_latex_chars"] == 65_536

    too_large = tools.invoke("range_to_latex", {"sheet": "Sales", "range_ref": "A1:Z20"}).data
    assert too_large["ok"] is False
    assert too_large["type"] == "ToolInputError"
    assert "limit is 500" in too_large["error"]
