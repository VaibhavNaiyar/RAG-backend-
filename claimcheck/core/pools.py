"""Agent and skill pools: discovered by folder scan, never by manifest.

The catalogue cannot disagree with what is on disk, because it IS what is on
disk. A malformed agent or skill raises at load time instead of being silently
skipped — a dropped skill is one the planner never selects, which is far
harder to diagnose than a load error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from claimcheck.config import PKG, ROOT
from claimcheck.core.agentspec import AgentSpec, ContractError, ModelSpec, load_agent
from claimcheck.core.skill import SkillError, SkillScope, SkillSpec, load_skill

AGENT_POOL = PKG / "agents" / "pool"
SKILL_POOL = PKG / "skills" / "pool"

#: `**claim_verifier** (agent)` / `**search** (tool)` in a planner skill body.
_STEP = re.compile(r"\*\*([\w.]+)\*\*\s*\((agent|tool)\)", re.IGNORECASE)


@dataclass(frozen=True)
class ProcedureStep:
    name: str
    kind: str  # "agent" | "tool"
    position: int


def steps_of(skill: SkillSpec) -> tuple[ProcedureStep, ...]:
    """The steps a planner skill's procedure names, in order."""
    return tuple(
        ProcedureStep(name=m.group(1), kind=m.group(2).lower(), position=i)
        for i, m in enumerate(_STEP.finditer(skill.body), start=1)
    )


@lru_cache(maxsize=1)
def agents() -> dict[str, AgentSpec]:
    return {
        f.name: load_agent(f)
        for f in sorted(AGENT_POOL.iterdir())
        if f.is_dir() and (f / "agent.yaml").is_file()
    }


@lru_cache(maxsize=1)
def skills() -> dict[str, SkillSpec]:
    out = {}
    for f in sorted(SKILL_POOL.iterdir()):
        if f.is_dir() and (f / "SKILL.md").is_file():
            s = load_skill(f, scope=SkillScope.PLANNER)
            out[s.name] = s
    return out


def agent(name: str) -> AgentSpec:
    pool = agents()
    if name not in pool:
        raise ContractError(f"no agent {name!r}; available: {', '.join(pool)}")
    return pool[name]


def skill(name: str) -> SkillSpec:
    pool = skills()
    key = name if name in pool else name.replace("_", "-")
    if key not in pool:
        raise SkillError(f"no skill {name!r}; available: {', '.join(pool)}")
    return pool[key]


def skill_descriptions() -> list[str]:
    """Level 1: the entire planner-facing surface of the skill pool."""
    return [s.level_one() for s in skills().values()]


@lru_cache(maxsize=1)
def _routes() -> dict[str, str]:
    return (yaml.safe_load((ROOT / "config" / "models.yaml").read_text()) or {}).get("routes", {})


def resolve_model(spec: ModelSpec, settings) -> str:
    """`provider: gateway, name: fast` -> a litellm model string.

    Order: env override for the route (CC_FAST_MODEL ...) > config/models.yaml > literal name.
    """
    if spec.provider != "gateway":
        return f"{spec.provider}/{spec.name}"
    by_env = {"fast": settings.fast_model, "smart": settings.smart_model, "judge": settings.judge_model}
    return by_env.get(spec.name) or _routes().get(spec.name) or spec.name


def validate_pools() -> list[str]:
    """Cross-checks run in tests and at startup: every step a skill names exists."""
    problems = []
    known_tools = {"search"}
    for s in skills().values():
        for step in steps_of(s):
            if step.kind == "agent" and step.name not in agents():
                problems.append(f"skill {s.name}: step {step.name!r} is not an agent in the pool")
            if step.kind == "tool" and step.name not in known_tools:
                problems.append(f"skill {s.name}: step {step.name!r} is not a known tool")
        for d in s.extra.get("sources", []) or []:
            if not isinstance(d, str):
                problems.append(f"skill {s.name}: bad source {d!r}")
    return problems


def pool_paths() -> tuple[Path, Path]:
    return AGENT_POOL, SKILL_POOL
