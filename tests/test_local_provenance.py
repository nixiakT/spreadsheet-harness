from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Any

import pytest

from spreadsheet_harness import benchmark as benchmark_module


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("ascii")


def _private_record(
    *,
    local_scan_revision: str | None = None,
    pilot_first_committed_revision: str | None = None,
) -> tuple[dict[str, Any], str, str]:
    scan_revision = local_scan_revision or secrets.token_hex(20)
    pilot_revision = pilot_first_committed_revision or secrets.token_hex(20)
    while pilot_revision == scan_revision:
        pilot_revision = secrets.token_hex(20)
    payload: dict[str, Any] = {
        "schema_version": benchmark_module.TRACE2SKILL_LOCAL_PROVENANCE_SCHEMA_VERSION,
        "record_id": benchmark_module.TRACE2SKILL_LOCAL_PROVENANCE_RECORD_ID,
        "protocol_bindings": {
            "local_pool_manifest_id": (
                benchmark_module.TRACE2SKILL_LOCAL_UNATTEMPTED_MANIFEST_ID
            ),
            "pilot_manifest_id": benchmark_module.TRACE2SKILL_PILOT_MANIFEST_ID,
            "evidence_schema_version": (
                benchmark_module.TRACE2SKILL_LOCAL_EXPOSURE_EVIDENCE_SCHEMA_VERSION
            ),
        },
        "revisions": {
            "local_scan_revision": scan_revision,
            "pilot_first_committed_revision": pilot_revision,
        },
    }
    document = {
        **payload,
        "record_sha256": hashlib.sha256(_canonical_bytes(payload)).hexdigest(),
    }
    return document, scan_revision, pilot_revision


def _write_private_record(path: Path, document: dict[str, Any]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        data = _canonical_bytes(document)
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
    finally:
        os.close(descriptor)
    path.chmod(0o600)


def _rehash(document: dict[str, Any]) -> dict[str, Any]:
    payload = {key: value for key, value in document.items() if key != "record_sha256"}
    return {
        **payload,
        "record_sha256": hashlib.sha256(_canonical_bytes(payload)).hexdigest(),
    }


def test_private_local_provenance_loads_from_two_identical_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "local-provenance.json"
    document, scan_revision, pilot_revision = _private_record()
    _write_private_record(path, document)
    original_read = benchmark_module._read_local_provenance_snapshot
    reads = 0

    def counted_read(candidate: Path):
        nonlocal reads
        reads += 1
        return original_read(candidate)

    monkeypatch.setattr(
        benchmark_module,
        "_read_local_provenance_snapshot",
        counted_read,
    )

    provenance = benchmark_module._load_trace2skill_local_provenance(path)

    assert reads == 2
    assert provenance.local_scan_revision == scan_revision
    assert provenance.pilot_first_committed_revision == pilot_revision
    assert provenance.record_sha256 == document["record_sha256"]
    assert scan_revision not in repr(provenance)
    assert pilot_revision not in repr(provenance)


@pytest.mark.parametrize(
    "mutation",
    [
        "extra_top_level",
        "wrong_binding",
        "missing_revision",
        "uppercase_revision",
        "same_revision",
        "wrong_hash",
    ],
)
def test_private_local_provenance_rejects_nonexact_schema_or_values(
    tmp_path: Path,
    mutation: str,
) -> None:
    path = tmp_path / f"{mutation}.json"
    document, scan_revision, _ = _private_record()
    if mutation == "extra_top_level":
        document["unexpected"] = True
        document = _rehash(document)
    elif mutation == "wrong_binding":
        document["protocol_bindings"]["pilot_manifest_id"] = "substituted-protocol"
        document = _rehash(document)
    elif mutation == "missing_revision":
        document["revisions"].pop("pilot_first_committed_revision")
        document = _rehash(document)
    elif mutation == "uppercase_revision":
        document["revisions"]["local_scan_revision"] = scan_revision.upper()
        document = _rehash(document)
    elif mutation == "same_revision":
        document["revisions"]["pilot_first_committed_revision"] = scan_revision
        document = _rehash(document)
    else:
        document["record_sha256"] = secrets.token_hex(32)
    _write_private_record(path, document)

    with pytest.raises(ValueError, match="Private Trace2Skill local provenance"):
        benchmark_module._load_trace2skill_local_provenance(path)


def test_private_local_provenance_rejects_noncanonical_or_duplicate_json(
    tmp_path: Path,
) -> None:
    document, _, _ = _private_record()
    pretty = tmp_path / "pretty.json"
    pretty.write_text(json.dumps(document, indent=2) + "\n", encoding="ascii")
    pretty.chmod(0o600)

    with pytest.raises(ValueError, match="canonical hash"):
        benchmark_module._load_trace2skill_local_provenance(pretty)

    duplicate = tmp_path / "duplicate.json"
    raw = _canonical_bytes(document).replace(
        b'{"protocol_bindings":',
        b'{"record_id":"duplicate","protocol_bindings":',
        1,
    )
    duplicate.write_bytes(raw)
    duplicate.chmod(0o600)
    with pytest.raises(ValueError, match="strict JSON"):
        benchmark_module._load_trace2skill_local_provenance(duplicate)


@pytest.mark.parametrize("failure_mode", ["missing", "mode", "hardlink", "symlink"])
def test_private_local_provenance_requires_owner_only_unlinked_regular_file(
    tmp_path: Path,
    failure_mode: str,
) -> None:
    path = tmp_path / "local-provenance.json"
    document, _, _ = _private_record()
    if failure_mode != "missing":
        _write_private_record(path, document)
    candidate = path
    if failure_mode == "mode":
        path.chmod(0o640)
    elif failure_mode == "hardlink":
        os.link(path, tmp_path / "second-name.json")
    elif failure_mode == "symlink":
        link = tmp_path / "linked-provenance.json"
        link.symlink_to(path)
        candidate = link

    with pytest.raises(ValueError, match="unavailable or unsafe"):
        benchmark_module._load_trace2skill_local_provenance(candidate)


def test_private_local_provenance_rejects_symlinked_parent_directory(
    tmp_path: Path,
) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    path = real_parent / "local-provenance.json"
    document, _, _ = _private_record()
    _write_private_record(path, document)
    linked_parent = tmp_path / "linked-parent"
    try:
        linked_parent.symlink_to(real_parent, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(ValueError, match="unavailable or unsafe"):
        benchmark_module._load_trace2skill_local_provenance(
            linked_parent / path.name
        )


def test_private_local_provenance_rejects_change_between_snapshot_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "local-provenance.json"
    first_document, _, _ = _private_record()
    second_document, _, _ = _private_record()
    _write_private_record(path, first_document)
    original_read = benchmark_module._read_local_provenance_snapshot
    reads = 0

    def mutate_after_first_read(candidate: Path):
        nonlocal reads
        snapshot = original_read(candidate)
        reads += 1
        if reads == 1:
            candidate.write_bytes(_canonical_bytes(second_document))
            candidate.chmod(0o600)
        return snapshot

    monkeypatch.setattr(
        benchmark_module,
        "_read_local_provenance_snapshot",
        mutate_after_first_read,
    )

    with pytest.raises(ValueError, match="changed between snapshot reads"):
        benchmark_module._load_trace2skill_local_provenance(path)


def test_local_manifest_builder_fails_before_dataset_access_without_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset_accessed = False

    def forbidden_dataset_access(_: str | Path) -> dict[str, Any]:
        nonlocal dataset_accessed
        dataset_accessed = True
        raise AssertionError("dataset must not be read before private provenance")

    monkeypatch.setattr(
        benchmark_module,
        "trace2skill_heldout_manifest",
        forbidden_dataset_access,
    )

    with pytest.raises(ValueError, match="unavailable or unsafe"):
        benchmark_module.trace2skill_local_unattempted_manifest(
            tmp_path / "unread-dataset",
            local_provenance_path=tmp_path / "missing.json",
        )

    assert dataset_accessed is False


def test_split_preflight_requires_private_record_only_for_local_derivatives(
    tmp_path: Path,
) -> None:
    local_manifest = tmp_path / "local.json"
    local_manifest.write_text(
        json.dumps(
            {
                "schema_version": (
                    benchmark_module.TRACE2SKILL_DERIVATIVE_SPLIT_SCHEMA_VERSION
                ),
                "manifest_id": (
                    benchmark_module.TRACE2SKILL_LOCAL_UNATTEMPTED_MANIFEST_ID
                ),
            }
        ),
        encoding="utf-8",
    )
    canonical_heldout = Path(
        "benchmarks/protocols/qwen35-trace2skill-heldout-v1.json"
    )
    heldout_manifest = tmp_path / "heldout.json"
    heldout_manifest.write_bytes(canonical_heldout.read_bytes())
    assert "manifest_id" not in json.loads(heldout_manifest.read_text(encoding="utf-8"))

    with pytest.raises(ValueError, match="unavailable or unsafe"):
        benchmark_module.preflight_trace2skill_split_manifest(
            local_manifest,
            local_provenance_path=tmp_path / "missing.json",
        )

    preflight = benchmark_module.preflight_trace2skill_split_manifest(
        heldout_manifest,
        local_provenance_path=tmp_path / "missing.json",
    )

    assert preflight.local_provenance is None


def test_split_preflight_binds_manifest_and_private_record_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provenance_path = tmp_path / "local-provenance.json"
    document, scan_revision, _ = _private_record()
    _write_private_record(provenance_path, document)
    manifest_path = tmp_path / "local.json"
    local_document = {
        "schema_version": benchmark_module.TRACE2SKILL_DERIVATIVE_SPLIT_SCHEMA_VERSION,
        "manifest_id": benchmark_module.TRACE2SKILL_LOCAL_UNATTEMPTED_MANIFEST_ID,
    }
    manifest_path.write_text(json.dumps(local_document), encoding="utf-8")

    preflight = benchmark_module.preflight_trace2skill_split_manifest(
        manifest_path,
        local_provenance_path=provenance_path,
    )
    manifest_path.write_text(
        json.dumps(
            {"schema_version": benchmark_module.TRACE2SKILL_SPLIT_SCHEMA_VERSION}
        ),
        encoding="utf-8",
    )
    provenance_path.unlink()
    captured: dict[str, Any] = {}

    def fake_verify(
        dataset_root: str | Path,
        path: Path,
        frozen: dict[str, Any],
        manifest_hash: str,
        local_provenance: Any,
    ) -> dict[str, Any]:
        captured.update(
            {
                "dataset_root": dataset_root,
                "path": path,
                "frozen": frozen,
                "manifest_hash": manifest_hash,
                "local_provenance": local_provenance,
            }
        )
        return {"valid": True}

    monkeypatch.setattr(
        benchmark_module,
        "_verify_trace2skill_derivative_document",
        fake_verify,
    )
    monkeypatch.setattr(
        benchmark_module,
        "_load_trace2skill_local_provenance",
        lambda *_args, **_kwargs: pytest.fail("private record was reopened after preflight"),
    )

    report = benchmark_module.load_and_verify_trace2skill_split_manifest(
        tmp_path / "unread-dataset",
        manifest_path,
        preflight,
        local_provenance_path=provenance_path,
    )

    assert report == {"valid": True}
    assert captured["frozen"] == local_document
    assert captured["local_provenance"].local_scan_revision == scan_revision


def test_split_preflight_binds_heldout_classification_across_path_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "heldout.json"
    manifest_path.write_bytes(
        Path("benchmarks/protocols/qwen35-trace2skill-heldout-v1.json").read_bytes()
    )
    preflight = benchmark_module.preflight_trace2skill_split_manifest(manifest_path)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": (
                    benchmark_module.TRACE2SKILL_DERIVATIVE_SPLIT_SCHEMA_VERSION
                ),
                "manifest_id": (
                    benchmark_module.TRACE2SKILL_LOCAL_UNATTEMPTED_MANIFEST_ID
                ),
            }
        ),
        encoding="utf-8",
    )
    captured: dict[str, Any] = {}

    def fake_verify(
        dataset_root: str | Path,
        path: Path,
        frozen: dict[str, Any],
        manifest_hash: str,
    ) -> dict[str, Any]:
        captured["frozen"] = frozen
        return {"valid": True}

    monkeypatch.setattr(
        benchmark_module,
        "_verify_trace2skill_heldout_document",
        fake_verify,
    )
    monkeypatch.setattr(
        benchmark_module,
        "_load_trace2skill_local_provenance",
        lambda *_args, **_kwargs: pytest.fail("replacement changed split classification"),
    )

    report = benchmark_module.load_and_verify_trace2skill_split_manifest(
        tmp_path / "unread-dataset",
        manifest_path,
        preflight,
    )

    assert report == {"valid": True}
    assert captured["frozen"]["schema_version"] == (
        benchmark_module.TRACE2SKILL_SPLIT_SCHEMA_VERSION
    )
    assert "manifest_id" not in captured["frozen"]


def test_split_preflight_capability_cannot_be_reused_for_another_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    frozen = Path(
        "benchmarks/protocols/qwen35-trace2skill-heldout-v1.json"
    ).read_bytes()
    first.write_bytes(frozen)
    second.write_bytes(frozen)
    preflight = benchmark_module.preflight_trace2skill_split_manifest(first)
    monkeypatch.setattr(
        benchmark_module,
        "_verify_trace2skill_heldout_document",
        lambda *_args, **_kwargs: pytest.fail("mismatched capability reached verifier"),
    )

    with pytest.raises(ValueError, match="belongs to another path"):
        benchmark_module.load_and_verify_trace2skill_split_manifest(
            tmp_path / "unread-dataset",
            second,
            preflight,
        )


@pytest.mark.parametrize(
    "schema_version,manifest_id",
    [
        (benchmark_module.TRACE2SKILL_SPLIT_SCHEMA_VERSION, "unknown-heldout"),
        (
            benchmark_module.TRACE2SKILL_DERIVATIVE_SPLIT_SCHEMA_VERSION,
            "unknown-derivative",
        ),
        ("unknown-schema", benchmark_module.TRACE2SKILL_HELDOUT_MANIFEST_ID),
    ],
)
def test_split_preflight_rejects_unrecognized_manifest_before_private_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schema_version: str,
    manifest_id: str,
) -> None:
    manifest = tmp_path / "unrecognized.json"
    manifest.write_text(
        json.dumps({"schema_version": schema_version, "manifest_id": manifest_id}),
        encoding="utf-8",
    )
    private_read = False

    def forbidden_private_read(_: str | Path | None = None) -> Any:
        nonlocal private_read
        private_read = True
        raise AssertionError("unrecognized manifests must fail before private file access")

    monkeypatch.setattr(
        benchmark_module,
        "_load_trace2skill_local_provenance",
        forbidden_private_read,
    )

    with pytest.raises(ValueError, match="Unsupported"):
        benchmark_module.preflight_trace2skill_split_manifest(manifest)

    assert private_read is False


def test_split_verifier_passes_one_loaded_private_record_without_dataset_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provenance_path = tmp_path / "local-provenance.json"
    document, scan_revision, pilot_revision = _private_record()
    _write_private_record(provenance_path, document)
    manifest_path = tmp_path / "local.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": (
                    benchmark_module.TRACE2SKILL_DERIVATIVE_SPLIT_SCHEMA_VERSION
                ),
                "manifest_id": (
                    benchmark_module.TRACE2SKILL_LOCAL_UNATTEMPTED_MANIFEST_ID
                ),
            }
        ),
        encoding="utf-8",
    )
    captured: dict[str, Any] = {}

    def fake_verify(
        dataset_root: str | Path,
        path: Path,
        frozen: dict[str, Any],
        manifest_hash: str,
        local_provenance: Any,
    ) -> dict[str, Any]:
        captured.update(
            {
                "dataset_root": dataset_root,
                "path": path,
                "frozen": frozen,
                "manifest_hash": manifest_hash,
                "local_provenance": local_provenance,
            }
        )
        return {"valid": True}

    monkeypatch.setattr(
        benchmark_module,
        "_verify_trace2skill_derivative_document",
        fake_verify,
    )

    report = benchmark_module.load_and_verify_trace2skill_split_manifest(
        tmp_path / "unread-dataset",
        manifest_path,
        local_provenance_path=provenance_path,
    )

    assert report == {"valid": True}
    assert captured["dataset_root"] == tmp_path / "unread-dataset"
    assert captured["local_provenance"].local_scan_revision == scan_revision
    assert (
        captured["local_provenance"].pilot_first_committed_revision
        == pilot_revision
    )


def test_anonymous_source_has_no_legacy_local_git_revision_constants() -> None:
    source = Path(benchmark_module.__file__).read_text(encoding="utf-8")

    assert "TRACE2SKILL_LOCAL_SCAN_REVISION" not in source
    assert "TRACE2SKILL_PILOT_FIRST_COMMITTED_REVISION" not in source
