"""Turn an agent folder into a call. The only place agents meet a model.

    obj, usage = run_agent("claim_verifier", payload, settings, llm)

agent.yaml decides the model route and the output type; SKILL.md is the system
prompt. Optionally the prompt is pulled from Langfuse prompt management, so a
prompt can be changed, versioned and A/B-evaluated without a redeploy — the
local SKILL.md is always the fallback.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

from claimcheck import schemas, telemetry
from claimcheck.config import Settings
from claimcheck.core import pools
from claimcheck.llm import LLM
from claimcheck.schemas import StageUsage

log = logging.getLogger(__name__)

#: response_format name in agent.yaml -> pydantic type. A typo fails at load, not at call.
RESPONSE_FORMATS: dict[str, type[BaseModel]] = {
    "RouteDecision": schemas.RouteDecision,
    "Plan": schemas.Plan,
    "Answer": schemas.Answer,
    "VerifierReport": schemas.VerifierReport,
}

#: Short stage names used in spans, metrics and the FakeLLM.
STAGE = {
    "intent_router": "router",
    "planner": "planner",
    "compliance_answerer": "answerer",
    "claim_verifier": "verifier",
    "eval_judge": "judge",
}


class BudgetExceeded(RuntimeError):
    pass


def _system_prompt(name: str, local: str, settings: Settings) -> tuple[str, str]:
    """(prompt, version label). Langfuse first if enabled, local SKILL.md otherwise."""
    if settings.prompts_from_langfuse and (lf := telemetry.langfuse()):
        try:
            p = lf.get_prompt(f"claimcheck-{name}", label="production", fallback=local)
            return p.compile() if hasattr(p, "compile") else str(p.prompt), f"langfuse:v{getattr(p, 'version', '?')}"
        except Exception as e:  # noqa: BLE001
            log.warning("langfuse prompt %s unavailable (%s); using SKILL.md", name, e)
    return local, "local"


def run_agent(
    name: str,
    payload: dict[str, Any],
    settings: Settings,
    llm: LLM,
    *,
    extra_system: str = "",
) -> tuple[BaseModel, StageUsage]:
    spec = pools.agent(name)
    if spec.response_format not in RESPONSE_FORMATS:
        raise ValueError(f"agent {name}: unknown response_format {spec.response_format!r}")
    schema = RESPONSE_FORMATS[spec.response_format]
    model = pools.resolve_model(spec.model, settings) if spec.model else settings.smart_model
    system, version = _system_prompt(name, spec.system_prompt, settings)
    if extra_system:
        system = f"{system}\n\n{extra_system}"
    with telemetry.span(f"agent.{STAGE[name]}", agent=name, model=model, prompt_version=version) as s:
        obj, usage = llm.call(
            stage=STAGE[name], model=model, system=system, payload=payload, schema=schema,
            max_tokens=spec.model.max_tokens if spec.model else 1200,
            effort=spec.model.effort if spec.model else None,
        )
        telemetry.set_attrs(s, cost_usd=usage.cost_usd)
    telemetry.record_stage(STAGE[name], usage.latency_ms)
    cap = spec.budget.max_cost_usd
    if cap and usage.cost_usd > cap:
        raise BudgetExceeded(f"{name} spent ${usage.cost_usd:.4f} > budget ${cap}")
    return obj, usage
