"""Simple, auditable skill registry."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .errors import HarnessError

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass(frozen=True)
class Skill:
    name: str
    path: Path
    content: str
    sha256: str


class SkillRegistry:
    def __init__(self, roots: Iterable[Path], *, frozen: Iterable[Skill] | None = None) -> None:
        self.roots = [Path(root).resolve() for root in roots]
        self._frozen = tuple(frozen) if frozen is not None else None

    def freeze(self) -> SkillRegistry:
        """Snapshot skill contents so a long run cannot mix prompt versions."""

        return SkillRegistry((), frozen=self.discover())

    def discover(self) -> list[Skill]:
        if self._frozen is not None:
            return list(self._frozen)
        skills: list[Skill] = []
        seen: set[str] = set()
        for root in self.roots:
            if not root.exists():
                continue
            for path in sorted(root.glob("*/SKILL.md")):
                content = path.read_text(encoding="utf-8")
                match = _FRONTMATTER.match(content)
                name = path.parent.name
                if match:
                    for line in match.group(1).splitlines():
                        key, separator, value = line.partition(":")
                        if separator and key.strip() == "name":
                            name = value.strip().strip("\"'") or name
                if name in seen:
                    raise HarnessError(f"Duplicate skill name: {name}")
                seen.add(name)
                skills.append(
                    Skill(
                        name=name,
                        path=path,
                        content=content,
                        sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    )
                )
        return skills

    def render_for_prompt(self) -> tuple[str, list[dict[str, str]]]:
        sections: list[str] = []
        manifest: list[dict[str, str]] = []
        for skill in self.discover():
            sections.append(f"\n<skill name={skill.name!r}>\n{skill.content}\n</skill>")
            manifest.append({"name": skill.name, "path": str(skill.path), "sha256": skill.sha256})
        return "\n".join(sections), manifest
