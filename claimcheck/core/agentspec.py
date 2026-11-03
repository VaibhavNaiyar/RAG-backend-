"""agent.yaml schema — an agent is a folder, not a class.

    agents/pool/claim_verifier/
    ├── agent.yaml     wiring: model, tools, budget, response_format   engineer owns
    └── SKILL.md       how it does its one job                          domain expert owns

An agent is a model + a skill + a bounded tool subset. No base class, no
handler: adding a reasoning agent is dropping a folder. Changing how it
behaves is editing markdown.

The SKILL.md ``description`` is authoritative; agent.yaml must NOT duplicate it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from claimcheck.core._frontmatter import load_yaml
from claimcheck.core.skill import Axes, SkillScope, SkillSpec, load_skill

__all__ = ["ContractError", "ModelSpec", "Budget", "AgentSpec", "parse_agent_spec", "load_agent"]


class ContractError(ValueError):
    """An agent declaration is malformed or contradicts itself."""


class ModelSpec(BaseModel):
    """Which model this agent reasons with. `provider: gateway` + a route name;
    config/models.yaml maps the route to a concrete litellm model."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    provider: str
    name: str
    # How hard the model thinks before answering. Claude 5.5 models fix temperature, so this is
    # the per-agent behaviour knob; None leaves the model's own default.
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    max_tokens: int = Field(default=1200, gt=0)


class Budget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    max_steps: int = Field(default=4, gt=0)
    max_cost_usd: float | None = Field(default=None, gt=0)


class AgentSpec(BaseModel):
    """A parsed agent folder. ``tools`` is the complete set it may call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str = ""
    axes: Axes = Field(default_factory=Axes)
    model: ModelSpec | None = None
    skill: SkillSpec | None = None
    skill_path: str = "./SKILL.md"
    tools: tuple[str, ...] = ()
    subagents: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    budget: Budget = Field(default_factory=Budget)
    response_format: str | None = None
    writes: tuple[str, ...] = ()
    path: Path | None = None

    @field_validator("name")
    @classmethod
    def _snake_case(cls, value: str) -> str:
        if not value or not value.replace("_", "").isalnum() or value != value.lower():
            raise ValueError(f"Agent name {value!r} must be lower snake_case.")
        return value

    @property
    def system_prompt(self) -> str:
        return self.skill.body if self.skill else ""

    def level_one(self) -> str:
        return f"{self.name}: {self.description}"


def parse_agent_spec(source: str | dict[str, Any], *, path: Path | None = None,
                     skill: SkillSpec | None = None) -> AgentSpec:
    where = f" in {path}" if path else ""
    try:
        data = load_yaml(source) if isinstance(source, str) else dict(source)
    except Exception as exc:  # noqa: BLE001
        raise ContractError(f"Malformed agent.yaml{where}: {exc}") from exc
    if not data:
        raise ContractError(f"Empty agent.yaml{where}")
    name = str(data.pop("name", "") or "").strip()
    if not name:
        raise ContractError(f"agent.yaml is missing 'name'{where}")
    if "description" in data:
        raise ContractError(f"agent.yaml for {name!r}{where} declares 'description' — SKILL.md owns it.")
    model_raw = data.pop("model", None)
    if model_raw is not None and not isinstance(model_raw, dict):
        raise ContractError(f"agent.yaml 'model' for {name!r}{where} must be a mapping, got {model_raw!r}")
    budget_raw = data.pop("budget", None)
    if budget_raw is not None and not isinstance(budget_raw, dict):
        raise ContractError(f"agent.yaml 'budget' for {name!r}{where} must be a mapping, got {budget_raw!r}")
    axes = Axes.build(domains=data.pop("domains", None), purposes=data.pop("purposes", None),
                      modalities=data.pop("modalities", None))
    fields: dict[str, Any] = {
        "name": name,
        "axes": axes,
        "model": ModelSpec(**model_raw) if isinstance(model_raw, dict) else None,
        "skill": skill,
        "skill_path": str(data.pop("skill", "./SKILL.md") or "./SKILL.md"),
        "tools": tuple(data.pop("tools", ()) or ()),
        "subagents": tuple(data.pop("subagents", ()) or ()),
        "scopes": tuple(data.pop("scopes", ()) or ()),
        "budget": Budget(**budget_raw) if isinstance(budget_raw, dict) else Budget(),
        "response_format": data.pop("response_format", None),
        "writes": tuple(data.pop("writes", ()) or ()),
        "path": path.parent if path else None,
        "description": skill.description if skill else "",
    }
    if data:
        raise ContractError(f"Unrecognised agent.yaml keys for {name!r}{where}: {sorted(data)}")
    try:
        return AgentSpec(**fields)
    except Exception as exc:  # noqa: BLE001
        raise ContractError(f"Invalid agent.yaml for {name!r}{where}: {exc}") from exc


def load_agent(folder: str | Path) -> AgentSpec:
    directory = Path(folder)
    yaml_file = directory / "agent.yaml"
    if not yaml_file.is_file():
        raise ContractError(f"No agent.yaml in {directory}")
    stub = parse_agent_spec(yaml_file.read_text(encoding="utf-8"), path=yaml_file)
    skill = load_skill((directory / stub.skill_path).resolve(), scope=SkillScope.AGENT, owner=stub.name)
    spec = parse_agent_spec(yaml_file.read_text(encoding="utf-8"), path=yaml_file, skill=skill)
    if directory.name != spec.name:
        raise ContractError(f"Agent folder {directory.name!r} does not match declared name {spec.name!r}.")
    return spec
