"""The contracts between agents. Every hand-off is one of these, validated.

Typed hand-offs are the point: when the planner drifts, it fails validation at
the boundary instead of silently poisoning the answerer three steps later.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Route = Literal["compliance_question", "patient_medical_advice", "off_topic"]
Verdict = Literal["allowed", "not_allowed", "needs_review", "informational"]
Support = Literal["supported", "partial", "unsupported"]
Status = Literal["answered", "escalated", "refused"]


# ---- corpus ---------------------------------------------------------------
class Chunk(BaseModel):
    id: str
    doc_id: str
    doc_title: str
    kind: str
    page: int
    section: str
    text: str

    @property
    def citation(self) -> str:
        sec = f", {self.section}" if self.section else ""
        return f"{self.doc_title} — p.{self.page}{sec}"


class Hit(BaseModel):
    chunk: Chunk
    bm25_rank: int | None = None
    vector_rank: int | None = None
    rrf: float = 0.0
    rerank: float | None = None
    ref: str = ""  # "E1", "E2" ... the id the answerer cites


# ---- agent outputs --------------------------------------------------------
class RouteDecision(BaseModel):
    route: Route
    topic: Literal["label", "compounding", "advertising", "brand", "none"] = "none"
    reason: str = Field(description="One short sentence.")


class Plan(BaseModel):
    skill: str
    doc_ids: list[str] = Field(default_factory=list, description="Empty = search everything.")
    sub_queries: list[str] = Field(min_length=1, max_length=3)
    rationale: str


class Claim(BaseModel):
    text: str
    citations: list[str] = Field(default_factory=list, description="Evidence refs like E1, E3.")


class Answer(BaseModel):
    verdict: Verdict
    summary: str
    claims: list[Claim] = Field(default_factory=list)
    suggested_rewrite: str | None = None
    insufficient_evidence: bool = False


class ClaimCheck(BaseModel):
    index: int
    label: Support
    reason: str
    fix: str | None = Field(default=None, description="A rewrite fully supported by the evidence, if partial.")


class VerifierReport(BaseModel):
    checks: list[ClaimCheck]


class Verification(BaseModel):
    checks: list[ClaimCheck] = Field(default_factory=list)
    invalid_citations: list[str] = Field(default_factory=list)
    supported_fraction: float = 1.0
    removed: int = 0
    rewritten: int = 0
    escalate: bool = False
    skipped: bool = False


# ---- run record -----------------------------------------------------------
class StageUsage(BaseModel):
    stage: str
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0


class RunResult(BaseModel):
    run_id: str
    trace_id: str = ""
    mode: str = "pipeline"
    question: str
    status: Status
    route: RouteDecision | None = None
    plan: Plan | None = None
    hits: list[Hit] = Field(default_factory=list)
    draft: Answer | None = None
    verification: Verification | None = None
    final: Answer | None = None
    message: str = ""
    usage: list[StageUsage] = Field(default_factory=list)
    retried_unscoped: bool = False
    researched: bool = False  # thin evidence triggered one reformulated search

    @property
    def cost_usd(self) -> float:
        return round(sum(u.cost_usd for u in self.usage), 6)

    @property
    def latency_ms(self) -> float:
        return round(sum(u.latency_ms for u in self.usage), 1)
