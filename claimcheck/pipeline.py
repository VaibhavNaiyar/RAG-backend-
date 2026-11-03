"""The orchestrator: router -> planner -> [the selected skill's procedure].

The procedure is NOT hard-coded here. The planner picks a planner skill, and
this module executes the steps that skill's markdown names, in order:

    1. **search** (tool)
    2. **compliance_answerer** (agent)
    3. **claim_verifier** (agent)

So adding a fourth skill — or a skill that skips verification, or adds a
second retrieval pass — is a markdown change, validated at load time by
`pools.validate_pools()`.

Every stage is a span under one root span per question, so a single trace in
Tempo or Langfuse shows the whole run: route, plan, retrieved chunks with
their BM25/vector/rerank scores, the draft, each verifier label, cost.
"""

from __future__ import annotations

import re
import time
import uuid

from claimcheck import telemetry
from claimcheck.config import Settings
from claimcheck.core import pools
from claimcheck.llm import LLM, make_llm
from claimcheck.retrieve import Retriever
from claimcheck.runtime import BudgetExceeded, run_agent
from claimcheck.schemas import Answer, Hit, Plan, RouteDecision, RunResult
from claimcheck.verify import verify

# Deterministic guard before any model call: cheap, and not steerable by the input.
_INJECTION = re.compile(
    r"(ignore (all |any |the )?(previous|prior|above) (instructions|rules)|you are now|system prompt|"
    r"disregard (your|the) (rules|instructions)|act as (an? )?(unrestricted|jailbroken))",
    re.I,
)

REFUSALS = {
    "patient_medical_advice": (
        "This tool is for marketing-compliance questions, not personal medical advice. "
        "Please speak with your prescribing provider or a pharmacist."
    ),
    "off_topic": "This tool only answers questions about GLP-1 marketing compliance (labels, FDA/FTC guidance, brand rules).",
    "blocked": "This request looks like an attempt to override the tool's rules, so it was not processed.",
}
ESCALATION = "Not confident enough to answer from the sources — escalate to the compliance team."


def _evidence(hits: list[Hit]) -> list[dict]:
    return [
        {"ref": h.ref, "document": h.chunk.doc_title, "page": h.chunk.page, "section": h.chunk.section, "text": h.chunk.text}
        for h in hits
    ]


class Pipeline:
    def __init__(self, settings: Settings, llm: LLM | None = None, retriever: Retriever | None = None) -> None:
        self.s = settings
        self.llm = llm or make_llm(settings.llm_backend)
        self.retriever = retriever or Retriever(settings)

    # -- stages ----------------------------------------------------------------
    def route(self, q: str, run: RunResult) -> RouteDecision:
        if _INJECTION.search(q):
            return RouteDecision(route="off_topic", topic="none", reason="blocked: prompt-injection pattern")
        d, u = run_agent("intent_router", {"question": q}, self.s, self.llm)
        run.usage.append(u)
        return d  # type: ignore[return-value]

    def plan(self, q: str, route: RouteDecision, run: RunResult) -> Plan:
        payload = {
            "question": q,
            "topic": route.topic,
            "skills": pools.skill_descriptions(),
            "skill_sources": {n: s.extra.get("sources", []) for n, s in pools.skills().items()},
            "documents": self.retriever.index.catalogue(),
        }
        p, u = run_agent("planner", payload, self.s, self.llm)
        run.usage.append(u)
        assert isinstance(p, Plan)
        # Validate at the boundary: a hallucinated skill or doc_id never reaches retrieval.
        if p.skill not in pools.skills():
            p = p.model_copy(update={"skill": "claim-review", "rationale": f"(fallback from unknown skill {p.skill!r}) {p.rationale}"})
        known = set(self.retriever.index.doc_ids)
        doc_ids = [d for d in p.doc_ids if d in known]
        if pools.skill(p.skill).name == "claim-review" and "brand_rules" in known and "brand_rules" not in doc_ids and doc_ids:
            doc_ids.append("brand_rules")  # skill rule: a claim review always reads the brand rules
        return p.model_copy(update={"doc_ids": doc_ids})

    # -- run -------------------------------------------------------------------
    def run(self, question: str) -> RunResult:
        run = RunResult(run_id=uuid.uuid4().hex[:12], question=question, status="answered")
        t0 = time.perf_counter()
        with telemetry.span(
            "question",
            **{"langfuse.trace.name": "claimcheck.question", "langfuse.trace.input": question,
               "cc.mode": "pipeline", "cc.verifier": self.s.use_verifier},
        ) as root:
            run.trace_id = telemetry.current_trace_id()
            try:
                self._run(question, run)
            except BudgetExceeded as e:
                run.status, run.message = "escalated", f"{ESCALATION} ({e})"
            telemetry.set_attrs(
                root,
                **{"cc.status": run.status, "cc.route": run.route.route if run.route else "",
                   "cc.skill": run.plan.skill if run.plan else "", "cc.cost_usd": run.cost_usd,
                   "langfuse.trace.output": run.message},
            )
        wall = (time.perf_counter() - t0) * 1000
        telemetry.record_request(run.status, run.route.route if run.route else "", run.plan.skill if run.plan else "",
                                 "pipeline", run.cost_usd, wall)
        return run

    def _run(self, q: str, run: RunResult) -> None:
        run.route = self.route(q, run)
        if run.route.route != "compliance_question":
            key = "blocked" if run.route.reason.startswith("blocked") else run.route.route
            run.status, run.message = "refused", REFUSALS[key]
            return

        run.plan = self.plan(q, run.route, run)
        skill = pools.skill(run.plan.skill)
        draft: Answer | None = None

        for step in pools.steps_of(skill):
            if run.cost_usd > self.s.max_cost_usd:
                raise BudgetExceeded(f"run spent ${run.cost_usd:.4f} > ${self.s.max_cost_usd}")

            if step.name == "search":
                t = time.perf_counter()
                run.hits, run.retried_unscoped = self.retriever.search(
                    q, run.plan.sub_queries, run.plan.doc_ids, allowed=skill.extra.get("sources") or None)
                telemetry.record_stage("retrieve", (time.perf_counter() - t) * 1000)
                if not run.hits:
                    run.status, run.message = "escalated", ESCALATION + " (nothing retrieved)"
                    return

            elif step.name == "compliance_answerer":
                rules = skill.section("Rules")
                refs = "\n\n".join(
                    pools.agent("compliance_answerer").skill.read_reference(r)
                    for r in skill.extra.get("answerer_references", []) or []
                )
                extra = f"## Selected skill: {skill.name}\n{rules}" + (f"\n\n{refs}" if refs else "")
                if self.s.answerer_extra:  # eval-only: simulates a prompt regression
                    extra += f"\n\n{self.s.answerer_extra}"
                payload = {
                    "question": q,
                    "skill": skill.name,
                    "verdict_mode": skill.extra.get("verdict", "informational"),
                    "evidence": _evidence(run.hits),
                }
                d, u = run_agent("compliance_answerer", payload, self.s, self.llm, extra_system=extra)
                run.usage.append(u)
                draft = d  # type: ignore[assignment]
                run.draft = draft

            elif step.name == "claim_verifier":
                if not self.s.use_verifier:
                    from claimcheck.schemas import Verification
                    run.verification = Verification(skipped=True)
                    continue
                assert draft is not None, "skill puts claim_verifier before compliance_answerer"
                final, v, u = verify(draft, run.hits, self.s, self.llm)
                if u:
                    run.usage.append(u)
                run.verification, draft = v, final
                if v.escalate:
                    run.status = "escalated"

        run.final = draft
        if run.final is None:
            run.status, run.message = "escalated", ESCALATION
        elif run.status == "escalated":
            run.message = ESCALATION
        else:
            run.message = run.final.summary
