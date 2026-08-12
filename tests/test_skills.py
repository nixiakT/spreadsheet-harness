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
