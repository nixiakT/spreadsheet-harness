from __future__ import annotations

from pathlib import Path

from spreadsheet_harness.skills import SkillRegistry


def test_frozen_skill_registry_keeps_original_prompt(tmp_path: Path) -> None:
    skill_dir = tmp_path / "example"
    skill_dir.mkdir()
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text("---\nname: example\n---\noriginal\n", encoding="utf-8")
    frozen = SkillRegistry([tmp_path]).freeze()

    skill_path.write_text("---\nname: example\n---\nchanged\n", encoding="utf-8")

    rendered, manifest = frozen.render_for_prompt()
    assert "original" in rendered
    assert "changed" not in rendered
    assert manifest[0]["name"] == "example"


def test_spreadsheet_core_skill_keeps_task_independent_workflow() -> None:
    skill = (
        Path(__file__).parents[1] / "skills" / "spreadsheet-core" / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "Extend a verified adjacent formula with `fill_formula`" in skill
    assert "inspect the exact changed range and its immediate boundary" in skill
    heldout_derived_phrases = (
        "complete target range",
        "per-cell invariant",
        "every requested conditional branch",
        "zero `SUM` or `SUMIFS` result does not prove",
        "Exercise both conditional branches",
        "H6:P9",
        "SUM($E6:$G6)",
    )
    assert all(phrase not in skill for phrase in heldout_derived_phrases)


def test_spreadsheet_core_skill_chooses_formula_representation_safely() -> None:
    skill = (
        Path(__file__).parents[1] / "skills" / "spreadsheet-core" / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "requires values to remain live or updateable" in skill
    assert "exact formula behavior is supported by LibreOffice" in skill
    assert "materialized values for one-time cleaning, sorting, mapping, or aggregation" in skill
    assert "style, number format, and intended data type" in skill
    assert "immediately use a Calc-compatible formula" in skill
    assert "when live formulas are not required" in skill
    assert "known formula or recalculation error" in skill


def test_spreadsheet_core_skill_enforces_boundary_contract() -> None:
    skill = (
        Path(__file__).parents[1] / "skills" / "spreadsheet-core" / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "Never assume row 1 is a header" in skill
    assert "first and last target positions" in skill
    assert "immediately adjacent positions" in skill
    assert "title, header, section, subtotal, and total anchors" in skill
    assert "complete intended coverage" in skill
    assert "no residual matches" in skill
    assert "no changes beyond the target boundary" in skill
    assert "date and datetime values as typed dates" in skill
    assert "preserve original relative order within equal-key groups" in skill
