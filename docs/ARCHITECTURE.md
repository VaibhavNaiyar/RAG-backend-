# Architecture — what each piece is, and why it's there

## The problem shape

A marketing team at a GLP-1 telehealth company writes ads every day. Every
ad can break an FDA rule, an FTC rule, or the company's own stricter brand
rules. A wrong "allowed" is a regulatory incident. A wrong "not allowed" costs
a rewrite. So the system is built **asymmetrically**: it's cheap to be
cautious, and expensive to be confidently wrong.

That one fact drives most of the decisions below.

## Why an explicit pipeline (and a deep-agent mode to compare)

| | Explicit DAG (`pipeline.py`) | Deep agent (`deep.py`) |
|---|---|---|
| Control flow | Router → planner → the steps the skill names | The model decides when to search, read skills, delegate |
| Auditability | Every stage is a typed hand-off and a span | One long tool loop |
| Cost / latency | Fixed: 4 LLM calls | Variable: grows with every search the agent chooses |
| Failure attribution | Exact stage | "Somewhere in the loop" |
| Best for | Repeatable, regulated answers | Open-ended research |

Compliance answers have to be repeatable and auditable, so the DAG is the
default. Both modes share the **same skills, the same search tool, the same
verifier and the same goldens**. That makes `python -m evals.run --variants
baseline,deep` a fair comparison, and the choice is backed by numbers instead
of opinion.

## Agents are folders, skills are markdown

```
agents/pool/claim_verifier/
├── agent.yaml   model route, tools, budget, response_format   (engineer owns)
└── SKILL.md     how it does its one job                        (domain expert owns)

skills/pool/claim-review/SKILL.md   a procedure: which steps, which sources, which rules
```

- **Progressive disclosure.** The planner sees one line per skill (Level 1).
  The skill body enters context only after it's selected (Level 2).
  `references/*.md` load only when a skill asks for them (Level 3). Adding a
  skill doesn't make every request more expensive.
- **The procedure is data.** `steps_of()` extracts `**name** (agent|tool)` from
  the skill body, and the orchestrator runs those steps. A new procedure is a
  markdown change. `validate_pools()` fails at load time if a step names an
  agent that doesn't exist.
- **Contracts are enforced at load.** A planner skill that declares `tools` is
  rejected (if it needs tools, it's an agent). An agent.yaml that repeats the
  description is rejected (one router text, one place to edit it). A reference
  name supplied by the model is never treated as a path.
- **Model routes, not model names.** Agents say `name: fast`, and
  `config/models.yaml` maps that to a concrete model. The judge route is a
  stronger model (Opus) than the answerer it grades (Sonnet), so a weaker
  model never signs off on its own output. A different family is a one-line swap.

## Retrieval

1. **BM25 and vectors in parallel.** BM25 catches exact tokens like "2.4 mg",
   "BR-4" and "MEN 2". Vectors catch paraphrase like "say it works" ≈
   "efficacy claim".
2. **RRF fusion (k=60)** across both retrievers *and* across the planner's
   sub-queries. Rank-based, so raw scores never need calibrating.
3. **Cross-encoder rerank** (local MiniLM) against the original question.
4. **Margin cut.** Keep chunks within 0.35 of the best normalised score, with a
   minimum of 3 and a maximum of 6. A crisp answer isn't padded with noise.
5. **Scope widening.** If documents outside the planner's scope rank in the
   unscoped top 3, widen the scope. Widening is restricted to the selected
   skill's `sources`, and never reaches a sibling of a kind already in scope:
   if the planner chose the Wegovy label, it doesn't drift into the Zepbound
   label.
6. **Citations point somewhere a person can open:** document, page and section
   heading. Chunking is page-aware and section-aware for that reason.

## The verifier: an LLM labels, code decides

1. **Citation validity** in code, with 0 tokens. A claim citing nothing, or an
   E-ref that was never retrieved, is unsupported before a model sees it.
2. **`claim_verifier`** reads each claim against *only the text it cites* and
   labels it supported, partial (with a fix) or unsupported. That's narrower
   than "is this answer good?", which models tend to answer "yes".
3. **Policy in `verify.py`** decides what happens next: drop unsupported claims,
   swap in a fix for partial ones, and escalate when the supported share is
   below 0.6, nothing survives, or a verdict loses its support. Being code, the
   policy is unit-tested and identical on every run.

## Evals

- **Goldens** (`evals/goldens.yaml`, 25, hand-written): 5 topics plus refusals.
  Each golden specifies the expected route, skill, documents, key facts,
  forbidden phrases and verdict. They include deliberate traps: drug mix-ups
  (15 mg is Zepbound, not Wegovy), over-refusal ("what does the label say
  about pregnancy" is in scope), and prompt injection.
- **Metrics**, cheapest first: route, skill and retrieval recall, citation
  validity, key-fact recall, verdict accuracy and forbidden phrases are all
  deterministic. **Groundedness** is the only LLM metric, scored by `eval_judge`
  on the judge route against all retrieved context.
- **Attribution.** Each failure is pinned to the *first* stage that went wrong
  (router → planner → retrieval → answerer → verifier → grounding). Everything
  downstream was working from bad input, so the first failure points at the
  file to fix.
- **Variants** (`--variants`): `no_verifier`, `vector_only`, `bm25_only`,
  `no_rerank`, `deep`, and `regressed_prompt[_no_verifier]`. The last pair
  injects a "be more persuasive" prompt change and shows the verifier
  containing it.
- **Gate** (`--gate`): minimum and maximum thresholds plus a maximum regression
  against `baseline.json`. CI-ready.
- **DeepEval** (`evals/deepeval_suite.py`, optional): an independent
  faithfulness and relevancy cross-check of the judge.

## Observability

There's one OpenTelemetry `TracerProvider`. The OTLP exporter sends to the
Grafana LGTM stack (Tempo for traces, Prometheus for metrics). **Langfuse
attaches to the same provider** and reads the `gen_ai.*` and `langfuse.*` span
attributes, so there's no second instrumentation path to drift.

- Each question is one trace. The `cc.retrieve` span carries every hit with its
  scores. Each `cc.llm.*` span is a Langfuse generation with tokens and cost.
- Metrics cover requests by outcome, latency per stage, cost per stage and
  model, verifier supported fraction, removals and escalations, rerank top
  score and scope widenings, plus eval metrics per variant as gauges.
- Eval scores are also written onto each golden's Langfuse trace, so a red
  number is one click from the trace that caused it.
- **Steering with Langfuse.** `cli prompts push` uploads each agent's SKILL.md
  as a versioned prompt. With `CC_PROMPTS_FROM_LANGFUSE=true`, agents read the
  `production` label and fall back to the local file. Edit a prompt in
  Langfuse, run the goldens, compare, then promote the label.

## What's deliberately not here

No auth, no multi-tenant isolation, no conversation memory, no scheduler and
no ingestion connectors. None of them are needed to answer this problem well,
and every one would be a moving part to explain. `SCALING.md` covers where each
would come in.
