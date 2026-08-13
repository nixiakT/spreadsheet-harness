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


def test_spreadsheet_core_skill_excludes_heldout_derived_lessons() -> None:
    skill = (
        Path(__file__).parents[1] / "skills" / "spreadsheet-core" / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "Extend an adjacent formula with `fill_formula`" in skill
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
