"""Provider-agnostic structured LLM calls, each one a traced, costed span.

`LLM.call(...)` is the only way an agent talks to a model. It:
  1. renders the system prompt + a JSON payload,
  2. asks for JSON matching a pydantic schema,
  3. validates, and on failure retries ONCE with the validation error,
  4. records tokens, cost and latency on an OTel span (Langfuse "generation").

`FakeLLM` is a deterministic stand-in so the whole harness runs offline with
zero credits (tests, CI, dry-running the eval loop). It is a test double, not
a model: its answers are crude on purpose.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from claimcheck import telemetry
from claimcheck.schemas import StageUsage

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

# $ per 1M tokens (input, output). Only used when litellm has no price for a
# model name. Keep roughly current; it only affects the cost dashboards.
FALLBACK_PRICES = {
    "haiku": (1.0, 5.0),
    "sonnet": (3.0, 15.0),
    "opus": (5.0, 25.0),
    "gpt-5-mini": (0.25, 2.0),
    "gpt-5-nano": (0.05, 0.4),
    "gpt-4o-mini": (0.15, 0.6),
    "gpt-4.1-mini": (0.4, 1.6),
    "gemini": (0.3, 2.5),
    "fake": (0.0, 0.0),
}


def _price(model: str, tin: int, tout: int) -> float:
    for key, (pi, po) in FALLBACK_PRICES.items():
        if key in model:
            return (tin * pi + tout * po) / 1_000_000
    return (tin * 1.0 + tout * 5.0) / 1_000_000


def _extract_json(text: str) -> str:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


def _schema_hint(schema: type[BaseModel]) -> str:
    return json.dumps(schema.model_json_schema(), separators=(",", ":"))


class LLM:
    """litellm-backed. Any provider litellm supports: anthropic/, openai/, gemini/, bedrock/ ..."""

    name = "litellm"

    def call(
        self,
        *,
        stage: str,
        model: str,
        system: str,
        payload: dict[str, Any],
        schema: type[T],
        max_tokens: int = 1200,
        effort: str | None = None,
    ) -> tuple[T, StageUsage]:
        user = (
            "INPUT:\n" + json.dumps(payload, indent=1, ensure_ascii=False)
            + "\n\nRespond with ONLY a JSON object matching this JSON Schema (no prose, no fences):\n"
            + _schema_hint(schema)
        )
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        usage = StageUsage(stage=stage, model=model)
        t0 = time.perf_counter()
        with telemetry.span(
            f"llm.{stage}",
            **{
                "langfuse.observation.type": "generation",
                "gen_ai.system": model.split("/")[0],
                "gen_ai.request.model": model,
                "langfuse.observation.input": messages,
            },
        ) as s:
            last_err = ""
            for attempt in range(2):
                text, tin, tout, cost, finish = self._complete(model, messages, max_tokens, effort)
                usage.input_tokens += tin
                usage.output_tokens += tout
                usage.cost_usd += cost
                try:
                    obj = schema.model_validate_json(_extract_json(text))
                    break
                except (ValidationError, ValueError) as e:
                    last_err = str(e)[:600]
                    log.warning("%s: invalid JSON on attempt %d (finish=%s, out=%d tokens): %s",
                                stage, attempt + 1, finish, tout, last_err[:160])
                    telemetry.set_attrs(s, **{f"cc.attempt{attempt + 1}.finish_reason": finish})
                    messages = messages + [
                        {"role": "assistant", "content": text},
                        {"role": "user", "content": f"That failed validation:\n{last_err}\nReturn corrected JSON only."},
                    ]
            else:
                telemetry.set_attrs(s, error=last_err)
                raise RuntimeError(f"{stage}: model returned invalid JSON twice: {last_err[:200]}")
            usage.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
            telemetry.set_attrs(
                s,
                **{
                    "gen_ai.usage.input_tokens": usage.input_tokens,
                    "gen_ai.usage.output_tokens": usage.output_tokens,
                    "langfuse.observation.output": obj.model_dump(),
                    "langfuse.observation.cost_details": {"total": usage.cost_usd},
                    "cc.retries": attempt,
                },
            )
        telemetry.record_llm(stage, model, usage.input_tokens, usage.output_tokens, usage.cost_usd)
        return obj, usage

    def _complete(self, model: str, messages: list[dict], max_tokens: int,
                  effort: str | None) -> tuple[str, int, int, float, str]:
        import litellm

        litellm.drop_params = True  # drop params a provider does not support instead of failing
        # No temperature: Claude 5.5 models reject non-default sampling. reasoning_effort is litellm's
        # portable name for Anthropic's output_config.effort; thinking tokens count against max_tokens.
        extra = {"reasoning_effort": effort} if effort else {}
        resp = litellm.completion(model=model, messages=messages, max_tokens=max_tokens, **extra)
        text = resp.choices[0].message.content or ""
        tin = getattr(resp.usage, "prompt_tokens", 0) or 0
        tout = getattr(resp.usage, "completion_tokens", 0) or 0
        try:
            cost = float(litellm.completion_cost(completion_response=resp))
        except Exception:  # noqa: BLE001 - unknown model name in litellm's price map
            cost = _price(model, tin, tout)
        return text, tin, tout, cost, str(resp.choices[0].finish_reason)


# ---------------------------------------------------------------------------
# Offline test double
# ---------------------------------------------------------------------------
_WORD = re.compile(r"[a-z0-9][a-z0-9\-\.]+")
_STOP = set("the a an of to and or in on for is are be can we our with this that it as at by from not do does what which how when".split())


def _toks(s: str) -> set[str]:
    return {w for w in _WORD.findall(s.lower()) if w not in _STOP and len(w) > 2}


def _overlap(claim: str, evidence: str) -> float:
    c = _toks(claim)
    return len(c & _toks(evidence)) / max(1, len(c))


DOMAIN = set(
    "semaglutide tirzepatide wegovy zepbound ozempic mounjaro glp-1 glp ad ads advert claim claims compounded "
    "compounding label fda ftc boxed warning dose dosing testimonial marketing copy headline proven brand "
    "contraindication contraindicated thyroid pancreatitis rules weight approved clinically lose lbs guaranteed "
    "tagline instagram prescription evidence disclose doctor-designed program pregnancy".split()
)
PATIENT = re.compile(r"\b(should i|can i|my (dose|doctor|prescription)|i('m| am) (taking|on)|i have|for me|i feel)\b", re.I)
CLAIMY = re.compile(r"(\"|“|can we say|can we use|ad copy|headline|tagline|testimonial|before[/ -]and[/ -]after|our ad|in an ad|allowed to say|is it ok to)", re.I)


class FakeLLM(LLM):
    name = "fake"

    def call(self, *, stage, model, system, payload, schema, max_tokens=1200, effort=None):  # type: ignore[override]
        t0 = time.perf_counter()
        with telemetry.span(f"llm.{stage}", **{"gen_ai.request.model": "fake", "langfuse.observation.type": "generation"}):
            fn = getattr(self, f"_{stage}")
            obj = schema.model_validate(fn(payload, system) if stage == "answerer" else fn(payload))
        u = StageUsage(stage=stage, model="fake", input_tokens=len(json.dumps(payload)) // 4, output_tokens=60)
        u.latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        telemetry.record_llm(stage, "fake", u.input_tokens, u.output_tokens, 0.0)
        return obj, u

    def _router(self, p):
        q = p["question"]
        if PATIENT.search(q):
            return {"route": "patient_medical_advice", "topic": "none", "reason": "Personal medical question."}
        if not (_toks(q) & DOMAIN):
            return {"route": "off_topic", "topic": "none", "reason": "Not about GLP-1 marketing compliance."}
        topic = "compounding" if "compound" in q.lower() else "advertising" if CLAIMY.search(q) else "label"
        return {"route": "compliance_question", "topic": topic, "reason": "In scope."}

    def _planner(self, p):
        q = p["question"].lower()
        if CLAIMY.search(q):
            skill = "claim-review"
        elif "compound" in q:
            skill = "compounding-policy"
        elif any(w in q for w in ("ftc", "evidence", "establishment", "testimonial", "disclos", "advertis")):
            skill = "ad-rules"
        else:
            skill = "label-facts"
        docs = []
        if any(w in q for w in ("wegovy", "semaglutide")) and skill == "label-facts":
            docs.append("wegovy_label")
        if any(w in q for w in ("zepbound", "tirzepatide")) and skill == "label-facts":
            docs.append("zepbound_label")
        return {"skill": skill, "doc_ids": docs, "sub_queries": [p["question"]], "rationale": "keyword plan"}

    def _answerer(self, p, system=""):
        ev = p["evidence"][:3]
        claims = []
        for e in ev:
            sents = re.split(r"(?<=[.!?])\s+", e["text"])
            best = max(sents, key=lambda s: _overlap(p["question"], s)) if sents else e["text"]
            claims.append({"text": best[:300], "citations": [e["ref"]]})
        if p["skill"] == "claim-review":
            neg = any(re.search(r"\b(never|do not|not fda-approved|not allowed)\b", e["text"], re.I) for e in ev)
            verdict = "not_allowed" if neg else "needs_review"
        else:
            verdict = "informational"
        if "from your own knowledge" in system and ev:  # simulated prompt regression
            claims.append({"text": "Most patients see dramatic, lasting results within weeks.", "citations": [ev[0]["ref"]]})
        return {"verdict": verdict, "summary": claims[0]["text"] if claims else "No evidence.", "claims": claims,
                "insufficient_evidence": not ev}

    def _verifier(self, p):
        checks = []
        for i, c in enumerate(p["claims"]):
            ov = _overlap(c["text"], " ".join(c["evidence"]))
            label = "supported" if ov >= 0.6 else "partial" if ov >= 0.35 else "unsupported"
            checks.append({"index": c.get("index", i), "label": label, "reason": f"overlap={ov:.2f}"})
        return {"checks": checks}

    def _judge(self, p):
        ctx = " ".join(f"{c['section']} {c['text']}" if isinstance(c, dict) else c for c in p["context"])
        checks = []
        for i, c in enumerate(p["claims"]):
            ov = _overlap(c, ctx)
            checks.append({"index": i, "label": "supported" if ov >= 0.5 else "unsupported", "reason": f"overlap={ov:.2f}"})
        return {"checks": checks}


def make_llm(backend: str) -> LLM:
    return FakeLLM() if backend == "fake" else LLM()
