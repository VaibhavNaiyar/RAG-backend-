# CLAUDE.md — ClaimCheck

Compliance copilot for a GLP-1 telehealth marketing team. Agentic RAG with a
verifier, an eval harness, and OTel/Langfuse observability. Read
`docs/ARCHITECTURE.md` before changing anything structural.

## Commands

```bash
python -m claimcheck.cli pools          # load + validate every agent and skill (run after ANY pool edit)
python -m claimcheck.cli ask "..."      # one question, full trace printed
python -m evals.run --only ID1,ID2      # targeted goldens — cheap, use while iterating
python -m evals.run --variants a,b      # side-by-side comparison
python -m evals.run --gate              # must pass before you say "done"
pytest                                  # offline (fake LLM, fixture corpus), < 5 s
```

Offline mode for anything that doesn't need a real model:
`CC_LLM=fake CC_EMBEDDER=hash CC_RERANKER=lexical CC_SOURCES_FILE=tests/fixtures/sources.yaml CC_INDEX_DIR=index-test`

## How this codebase is shaped

- **Agents are folders** in `claimcheck/agents/pool/<name>/`: `agent.yaml`
  (wiring) + `SKILL.md` (behaviour). No Python in a pool folder, ever.
  Behaviour changes go in SKILL.md; model changes go in `config/models.yaml`.
- **Planner skills** live in `claimcheck/skills/pool/<name>/SKILL.md`. The folder
  name equals the `name:`. Steps are written `**name** (agent|tool)` and are
  executed in that order. A planner skill never declares `tools`/`model`.
- The SKILL.md `description` is the router. Write it in the words users type.
  When the planner misroutes, fix the description first, not the planner.
- Every hand-off is a pydantic model in `schemas.py`. Validate at the boundary,
  never deep inside a stage.
- New stages get a `telemetry.span("...")` and a metric. If it isn't traced, it
  didn't happen.

## Rules for changes

1. **Evals first.** Before fixing a failure, find its golden (or write one) and
   run it red. Then fix. Then run it green, plus `--gate`.
2. **Never edit a golden to make a run green.** If a golden is wrong, say why in
   the commit and change it on its own.
3. **Thresholds only ratchet up.**
4. Fix the **first** failing stage `evals.run` reports. Downstream failures are
   usually caused by it.
5. Don't add infrastructure the problem doesn't need (auth, memory, schedulers).
   Note it in `docs/SCALING.md` instead.
6. Retrieval and embeddings stay local (fastembed). Don't add a paid API to the
   retrieval path without an eval showing it's needed.

## Domain guardrails

- This is an internal marketing tool. It never gives patients medical advice.
  The router refuses those questions.
- A compounded drug is never "FDA-approved", "the same as" a branded drug, or
  "clinically proven". These are hard rules, not style.
- When unsure, the verdict is `needs_review`. A false "allowed" is the worst
  outcome this system can produce.
