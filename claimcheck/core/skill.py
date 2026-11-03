"""SKILL.md parsing — one SkillSpec, two scopes.

There is exactly ONE procedural primitive: the skill. It appears at two scopes,
and the difference between them is ownership, not type:

    PLANNER SKILL   skills/pool/<name>/SKILL.md     multi-step procedure
                    "what sequence handles this request"
                    Markdown only. No model, no tools, no scopes.

    AGENT SKILL     agents/pool/<name>/SKILL.md     one capability
                    "how do I do my one job"
                    Ships beside an agent.yaml, which holds the wiring.

The frontmatter ``description`` is authoritative. It is what the planner matches
on, it is the ONLY thing loaded at Level 1, and it is therefore the
highest-leverage text in the system. State what it does AND when to use it.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from claimcheck.core._frontmatter import load_yaml, split_frontmatter

__all__ = ["SkillError", "SkillScope", "SkillSpec", "Axes", "parse_skill", "load_skill"]

#: Keys that belong to an agent's wiring (agent.yaml), never to a skill.
_WIRING_KEYS = frozenset({"tools", "scopes", "model", "budget", "response_format", "subagents"})
_REFERENCES_DIR = "references"
#: Description soft limit — it sits in planner context on every single request.
_DESCRIPTION_MAX = 1024


class SkillError(ValueError):
    """A SKILL.md is missing, malformed, or violates the scope rules."""


class SkillScope(str, Enum):
    PLANNER = "planner"
    AGENT = "agent"


def _axis(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    items = [value] if isinstance(value, str) else list(value)
    items = [str(i).strip().lower() for i in items if str(i).strip()]
    return () if "any" in items else tuple(items)


class Axes(BaseModel):
    """Where an item is eligible. Empty on an axis (or `[any]`) = every value."""

    model_config = ConfigDict(frozen=True)

    domains: tuple[str, ...] = ()
    purposes: tuple[str, ...] = ()
    modalities: tuple[str, ...] = ()

    @classmethod
    def build(cls, *, domains: Any = None, purposes: Any = None, modalities: Any = None) -> Axes:
        return cls(domains=_axis(domains), purposes=_axis(purposes), modalities=_axis(modalities))

    def admits(self, axis: str, value: str) -> bool:
        allowed = getattr(self, axis)
        return not allowed or value in allowed


class SkillSpec(BaseModel):
    """A parsed SKILL.md — frontmatter plus the procedure body.

    Level 1 loading reads ``name`` and ``description`` only. The ``body`` is
    Level 2 and enters context only after selection.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str
    body: str = ""
    axes: Axes = Field(default_factory=Axes)
    scope: SkillScope = SkillScope.PLANNER
    owner: str | None = None
    path: Path | None = None
    extra: dict[str, Any] = Field(default_factory=dict, description="Unrecognised frontmatter, verbatim.")

    @model_validator(mode="after")
    def _check_scope_consistency(self) -> SkillSpec:
        if self.scope is SkillScope.AGENT and not self.owner:
            raise ValueError("An agent-scoped skill must name its owning agent.")
        if self.scope is SkillScope.PLANNER and self.owner:
            raise ValueError("A planner skill has no owning agent.")
        return self

    @property
    def references(self) -> tuple[str, ...]:
        """Level 3 files in references/. Never in context unless the body asks."""
        if self.path is None:
            return ()
        folder = self.path.parent / _REFERENCES_DIR
        if not folder.is_dir():
            return ()
        return tuple(sorted(p.name for p in folder.glob("*.md") if p.is_file()))

    def read_reference(self, name: str) -> str:
        """Only a name `references` lists is served — a model string never becomes a path."""
        if name not in self.references:
            raise SkillError(f"skill {self.name!r} has no reference {name!r}; available: {', '.join(self.references) or 'none'}")
        assert self.path is not None
        return (self.path.parent / _REFERENCES_DIR / name).read_text(encoding="utf-8")

    def section(self, heading: str) -> str:
        """The text under `## <heading>` in the body, up to the next `## `."""
        out, on = [], False
        for line in self.body.splitlines():
            if line.startswith("## "):
                on = line[3:].strip().lower() == heading.lower()
                continue
            if on:
                out.append(line)
        return "\n".join(out).strip()

    def level_one(self) -> str:
        """The ~15-token line that sits in planner context for every request."""
        return f"{self.name}: {self.description}"


def parse_skill(text: str, *, scope: SkillScope = SkillScope.PLANNER, owner: str | None = None,
                path: Path | None = None) -> SkillSpec:
    where = f" in {path}" if path else ""
    raw, body = split_frontmatter(text)
    if not raw.strip():
        raise SkillError(f"SKILL.md has no frontmatter{where}; name and description are required.")
    try:
        front = load_yaml(raw)
    except Exception as exc:  # noqa: BLE001
        raise SkillError(f"Malformed SKILL.md frontmatter{where}: {exc}") from exc

    name = str(front.pop("name", "") or "").strip()
    description = " ".join(str(front.pop("description", "") or "").split()).strip()
    if not name:
        raise SkillError(f"SKILL.md frontmatter is missing 'name'{where}.")
    if not description:
        raise SkillError(f"SKILL.md frontmatter is missing 'description'{where} — the description is the router.")
    if len(description) > _DESCRIPTION_MAX:
        raise SkillError(f"SKILL.md description is {len(description)} chars{where}; limit is {_DESCRIPTION_MAX}.")

    if scope is SkillScope.PLANNER:
        present = _WIRING_KEYS.intersection(front)
        if present:
            raise SkillError(
                f"Planner skill {name!r}{where} declares wiring keys {sorted(present)}. "
                "A planner skill has no model, tools or scopes — if it needs them, it is an agent."
            )
    axes = Axes.build(domains=front.pop("domains", None), purposes=front.pop("purposes", None),
                      modalities=front.pop("modalities", None))
    for key in _WIRING_KEYS:
        front.pop(key, None)
    return SkillSpec(name=name, description=description, body=body.strip(), axes=axes, scope=scope,
                     owner=owner, path=path, extra=front)


def load_skill(path: str | Path, *, scope: SkillScope = SkillScope.PLANNER, owner: str | None = None) -> SkillSpec:
    file = Path(path)
    if file.is_dir():
        file = file / "SKILL.md"
    if not file.is_file():
        raise SkillError(f"No SKILL.md at {file}")
    return parse_skill(file.read_text(encoding="utf-8"), scope=scope, owner=owner, path=file)
