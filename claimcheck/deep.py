"""The alternative architecture: one deepagents agent, same skills, same tools.

    python -m claimcheck.cli ask --mode deep "Can we say ..."
    python -m evals.run --mode deep

Instead of an explicit router -> planner -> steps DAG, a single deep agent gets:
  * the SAME planner skills (skills/pool/*/SKILL.md), loaded by deepagents'
    own progressive-disclosure skill loader,
  * ONE tool, `search_corpus` (the same Retriever),
  * the claim_verifier agent folder as a deepagents subagent it may delegate to,
  * the same `Answer` response schema.

Its output then goes through the SAME deterministic `verify()` and the SAME
goldens, so the comparison is architecture vs architecture, nothing else.

This is the only module that imports deepagents / langchain — the churn of a
young framework stays in one file.
"""

from __future__ import annotations

import json
import time
import uuid

from claimcheck import telemetry
from claimcheck.config import PKG, Settings
from claimcheck.core import pools
from claimcheck.llm import LLM, _price, make_llm
from claimcheck.pipeline import ESCALATION, REFUSALS, _INJECTION, _evidence
from claimcheck.retrieve import Retriever
from claimcheck.schemas import Answer, Hit, RouteDecision, RunResult, StageUsage
from claimcheck.verify import verify

SYSTEM = """You are the compliance copilot for a telehealth company's INTERNAL marketing team.

1. If the question is a patient asking about their own treatment, or is not about GLP-1
   marketing compliance, return verdict "informational", no claims, and a summary that
   politely declines. Do not search.
2. Otherwise read the ONE skill (in /pool/) whose description fits, and follow its procedure.
3. Use `search_corpus` to retrieve evidence. You may search at most 3 times.
4. Every claim cites evidence refs (E1, E2...) exactly as returned by search_corpus.
   Never state anything the evidence does not.
5. You may delegate a draft to the `claim_verifier` subagent before answering.

Document ids you can scope a search to: {docs}
"""


def _to_langchain(model: str) -> str:
    """litellm 'anthropic/claude-x' -> langchain 'anthropic:claude-x'."""
    return model.replace("/", ":", 1) if "/" in model else model


def build(settings: Settings, retriever: Retriever, collected: list[Hit]):
    from deepagents import create_deep_agent
    from deepagents.backends import FilesystemBackend
    from langchain_core.tools import tool

    @tool
    def search_corpus(query: str, doc_ids: list[str] | None = None) -> str:
        """Hybrid search of the compliance corpus. Returns numbered evidence passages
        (E1, E2, ...) with document, page and section. Optionally scope with doc_ids."""
        hits, _ = retriever.search(query, [query], doc_ids or [])
        # Renumber globally so refs stay unique across several searches.
        seen = {h.chunk.id for h in collected}
        for h in hits:
            if h.chunk.id not in seen:
                h.ref = f"E{len(collected) + 1}"
                collected.append(h)
                seen.add(h.chunk.id)
            else:
                h.ref = next(c.ref for c in collected if c.chunk.id == h.chunk.id)
        return json.dumps(_evidence(hits), ensure_ascii=False)

    verifier = pools.agent("claim_verifier")
    model = pools.resolve_model(pools.agent("compliance_answerer").model, settings)
    agent = create_deep_agent(
        model=_to_langchain(model),
        tools=[search_corpus],
        system_prompt=SYSTEM.format(docs=", ".join(retriever.index.doc_ids)),
        skills=["/pool/"],
        backend=FilesystemBackend(root_dir=str(PKG / "skills"), virtual_mode=True),
        subagents=[{
            "name": verifier.name,
            "description": verifier.description,
            "system_prompt": verifier.system_prompt,
            "model": _to_langchain(pools.resolve_model(verifier.model, settings)),
            "tools": [],
        }],
        response_format=Answer,
        name="claimcheck_deep",
    )
    return agent, model


class DeepPipeline:
    """Same interface as Pipeline.run(), so the eval harness can swap it in."""

    def __init__(self, settings: Settings, llm: LLM | None = None, retriever: Retriever | None = None) -> None:
        self.s = settings
        self.llm = llm or make_llm(settings.llm_backend)
        self.retriever = retriever or Retriever(settings)

    def run(self, question: str) -> RunResult:
        from langchain_core.callbacks import UsageMetadataCallbackHandler

        run = RunResult(run_id=uuid.uuid4().hex[:12], question=question, status="answered", mode="deep")
        t0 = time.perf_counter()
        with telemetry.span("question", **{"langfuse.trace.name": "claimcheck.question.deep",
                                           "langfuse.trace.input": question, "cc.mode": "deep"}) as root:
            run.trace_id = telemetry.current_trace_id()
            if _INJECTION.search(question):
                run.route = RouteDecision(route="off_topic", reason="blocked: prompt-injection pattern")
                run.status, run.message = "refused", REFUSALS["blocked"]
            else:
                collected: list[Hit] = []
                agent, model = build(self.s, self.retriever, collected)
                cb = UsageMetadataCallbackHandler()
                with telemetry.span("agent.deep", model=model):
                    out = agent.invoke({"messages": [{"role": "user", "content": question}]},
                                       config={"callbacks": [cb], "recursion_limit": 40})
                tin = sum(v.get("input_tokens", 0) for v in cb.usage_metadata.values())
                tout = sum(v.get("output_tokens", 0) for v in cb.usage_metadata.values())
                run.usage.append(StageUsage(stage="deep_agent", model=model, input_tokens=tin, output_tokens=tout,
                                            cost_usd=_price(model, tin, tout),
                                            latency_ms=round((time.perf_counter() - t0) * 1000, 1)))
                draft: Answer | None = out.get("structured_response")
                run.hits, run.draft = collected, draft
                if draft is None:
                    run.status, run.message = "escalated", ESCALATION
                elif not collected and not draft.claims:
                    run.route = RouteDecision(route="off_topic", reason="deep agent declined without searching")
                    run.status, run.message, run.final = "refused", draft.summary, draft
                else:
                    run.route = RouteDecision(route="compliance_question", reason="deep agent searched")
                    if self.s.use_verifier:
                        final, v, u = verify(draft, collected, self.s, self.llm)
                        if u:
                            run.usage.append(u)
                        run.final, run.verification = final, v
                        if v.escalate:
                            run.status = "escalated"
                    else:
                        run.final = draft
                    run.message = ESCALATION if run.status == "escalated" else run.final.summary
            telemetry.set_attrs(root, **{"cc.status": run.status, "cc.cost_usd": run.cost_usd})
        telemetry.record_request(run.status, run.route.route if run.route else "", "deep", "deep",
                                 run.cost_usd, (time.perf_counter() - t0) * 1000)
        return run
