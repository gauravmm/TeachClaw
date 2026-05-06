"""Skills loader for agent capabilities."""

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SkillInfo:
    name: str
    path: Path
    content: str  # full raw file content
    body: str  # content with frontmatter stripped
    metadata: dict  # frontmatter key/value pairs


class SkillsLoader:
    """Loads agent skills from the workspace skills directory."""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.skills_dir = workspace / "skills"

    def get_all_skills(self) -> dict[str, SkillInfo]:
        """Return all skills keyed by name, including metadata and full content."""
        skills: dict[str, SkillInfo] = {}
        if not self.skills_dir.exists():
            return skills

        for skill_dir in self.skills_dir.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue

            content = skill_file.read_text(encoding="utf-8")
            metadata, body = self._split_frontmatter(content)

            skills[skill_dir.name] = SkillInfo(
                name=skill_dir.name,
                path=skill_file.relative_to(self.workspace),
                content=content,
                body=body,
                metadata=metadata,
            )

        return skills

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _split_frontmatter(self, content: str) -> tuple[dict, str]:
        """Parse frontmatter and return (metadata dict, body string)."""
        if content.startswith("---"):
            match = re.match(r"^---\n(.*?)\n---\n?", content, re.DOTALL)
            if match:
                metadata: dict = {}
                for line in match.group(1).split("\n"):
                    if ":" in line:
                        key, value = line.split(":", 1)
                        metadata[key.strip()] = value.strip().strip("\"'")
                return metadata, content[match.end() :].strip()
        return {}, content
