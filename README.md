# ClaimCheck

**A compliance copilot for a telehealth marketing team.** Ask *"Can we say
'clinically proven' for compounded semaglutide?"* and get a verdict, a cited
answer and a compliant rewrite. The answer comes only from FDA labels, FDA
guidance, FTC guidance and internal brand rules. When the sources don't settle
the question, it escalates to a human.

It's an agentic RAG system built to be **measured**. Every hand-off is typed,
every stage is a trace span, and every change runs against a hand-written
golden set. A failure is attributed to the stage that caused it.

```
question
   â”‚
   â–¼
[guard] regex prompt-injection check (no model, not steerable)
   â”‚
   â–¼
intent_router  (fast)  â”€â”€ patient_medical_advice / off_topic â”€â”€â–¶ refuse
   â”‚ compliance_question
   â–¼
planner        (fast)  picks a planner SKILL + doc scope + 1-3 sub-queries
   â”‚
   â–¼  executes the steps that SKILL.md names, in order:
   1. search (tool)        BM25 + vectors â†’ RRF â†’ cross-encoder rerank â†’ margin
                           scope widens only into the skill's own sources
   2. compliance_answerer  (smart)  verdict + one-sentence claims, each citing E-refs
   3. claim_verifier       (fast)   code checks citations (0 tokens), then the LLM labels each claim;
                                    code drops / rewrites / escalates
   â”‚
   â–¼
answer  Â·  OTel spans â†’ Tempo + Langfuse  Â·  OTel metrics â†’ Prometheus â†’ Grafana
```

## Repo map

| Path | What it is |
|---|---|
| `claimcheck/agents/pool/<agent>/` | **An agent is a folder:** `agent.yaml` (model route, tools, budget, response type) + `SKILL.md` (how it does its one job). No Python. |
| `claimcheck/skills/pool/<skill>/SKILL.md` | **Planner skills:** a procedure in markdown. The planner sees only the `description` (Level 1). `**name** (agent\|tool)` steps are executed in order. |
| `claimcheck/core/` | Loaders for the two formats above, with contract checks (a planner skill may not declare tools; agent.yaml may not duplicate the description; references are whitelisted). |
| `config/models.yaml` | Model routes: `fast`, `smart`, `judge`. Swapping a model is a one-line diff. |
| `claimcheck/retrieve.py` | Hybrid search, RRF, rerank, margin, scope widening. |
| `claimcheck/verify.py` | Deterministic citation check + LLM labels + policy in code. |
| `claimcheck/pipeline.py` | Orchestrator. Runs whatever steps the selected skill declares. |
| `claimcheck/deep.py` | Same skills and tool, one `deepagents` agent. Used for architecture comparison. |
| `claimcheck/telemetry.py` | One OTel provider feeds both Grafana (OTLP) and Langfuse. |
| `evals/` | Goldens, metrics, stage attribution, ablation variants, release gate. |
| `corpus/` | `sources.yaml` (one doc per topic), `brand-rules.md`. |
| `docs/` | `KT.md` (handover: start here), `ARCHITECTURE.md` (why), `SCALING.md` (what changes at 10k docs), `DEMO_SCRIPT.md`. |

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[local,langfuse,dev]'          # add ,deep for the deepagents mode
cp .env.example .env                            # add ANTHROPIC_API_KEY (used by every agent, including the judge)

python scripts/fetch_corpus.py                  # 4 public docs; prints a manual link if a site blocks it
python -m claimcheck.cli ingest                 # parse â†’ chunk â†’ embed (local, free)
python -m claimcheck.cli pools                  # agents + skills as the planner sees them
python -m claimcheck.cli ask 'Can we say "clinically proven" for compounded semaglutide?'

python -m claimcheck.cli serve                  # UI at http://localhost:8000
```

Observability (optional):

```bash
docker compose up -d && python scripts/import_dashboard.py   # Grafana at :3000 â†’ "ClaimCheck"
# Langfuse: set LANGFUSE_PUBLIC_KEY / SECRET_KEY in .env (cloud free tier)
python -m claimcheck.cli prompts push                        # SKILL.md bodies â†’ Langfuse prompt management
```

Evals:

```bash
python -m evals.run                                          # 25 goldens, baseline
python -m evals.run --variants baseline,no_verifier,bm25_only
python -m evals.run --variants regressed_prompt,regressed_prompt_no_verifier   # the drift demo
python -m evals.run --variants baseline,deep                 # explicit DAG vs deep agent
python -m evals.run --gate                                   # CI: exit 1 on threshold/regression
```

Offline, with no keys and no credits, everything runs against a synthetic
fixture corpus and a deterministic fake LLM:

```bash
CC_LLM=fake CC_EMBEDDER=hash CC_RERANKER=lexical CC_SOURCES_FILE=tests/fixtures/sources.yaml \
  CC_INDEX_DIR=index-test python -m evals.run --variants regressed_prompt,regressed_prompt_no_verifier
pytest
```

## Cost

Per question: 3 calls on the fast route plus 1 on the smart route, about
**$0.02â€“0.03** with Haiku and Sonnet. Embeddings and reranking run locally
(fastembed, ONNX), so they cost $0. A full 25-golden eval run with the judge
costs about $0.75. Set `CC_SMART_MODEL=anthropic/claude-haiku-5-5` to cut that
by about 3x while iterating.

> Demo project. The brand rules are invented and simplified. Nothing here is
> legal advice, and every real answer goes to a human compliance reviewer.
