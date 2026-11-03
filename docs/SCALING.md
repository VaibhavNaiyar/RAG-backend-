# Scaling: from 5 documents to a real corpus

The demo has one document per topic on purpose: the goal was to prove the
*harness*, not the storage. Here's what changes as the corpus grows, and what
doesn't.

## What stays the same

The agent and skill folders, typed hand-offs, the verifier, the goldens, the
metrics, stage attribution and the OTel/Langfuse instrumentation. That's the
point of building the harness first: everything below is a swap behind an
interface the evals already measure.

## What changes

| Concern | Demo (5 docs, ~300 chunks) | Real corpus (thousands of docs, every label revision, state rules, past reviews) |
|---|---|---|
| **Index** | `chunks.jsonl` + `vectors.npy` in memory, BM25 rebuilt at load | Postgres + pgvector (HNSW) with `tsvector` BM25, or OpenSearch hybrid. Same RRF on top. |
| **Ingest** | One CLI run | An incremental job: hash each document, re-embed only what changed, keep label **versions** with effective dates. A compliance answer must be able to say "per the 2025-03 label". |
| **Parsing** | pypdf + heading regex | A layout-aware parser (Docling or similar) for tables (dosing tables, adverse-reaction tables) and the label's numbered section tree. |
| **Scoping** | Planner picks from a 5-row catalogue | A `doc.list` tool with metadata filters (drug, document type, jurisdiction, effective date). The planner scopes by filter, not by naming every document. |
| **Rerank** | Local MiniLM on CPU | Same model batched on a GPU, or a hosted reranker. Rerank only the top ~50 fused candidates either way. |
| **Embeddings** | bge-small, local | Keep local for cost, or a larger model if retrieval recall on the goldens says so. **Decided by the `vector_only` variant, not by taste.** |
| **Goldens** | 25 hand-written | Seed from these, then grow from production: every escalation, thumbs-down or reviewer override becomes a candidate golden. Split into smoke (on every PR), full (nightly) and adversarial suites. |
| **Online evals** | Verifier on every answer | Same, plus sampled judge scoring of live traffic in Langfuse, with alerts on drift in groundedness or escalation rate. |
| **Cost** | ~$0.025 per question (estimate) | Prompt caching on the long, static skill and system text. Route easy label lookups to the fast model end to end. A per-tenant budget in the guard. |
| **Latency** | 4 sequential calls | Run the router and a speculative retrieval in parallel. Stream the answer and verify claims as they arrive. Cache answers for repeat copy. |
| **Human loop** | "Escalate" is a message | A review queue. A reviewer's decision is stored and fed back as a regression golden and a few-shot example. |
| **Multi-team** | One brand-rules file | Brand rules per business line as their own documents, with the planner scoping by tenant. Same skills. |

## The order I'd do it in

1. Version-aware ingest plus a pgvector index. Correctness first: answers need
   effective dates.
2. Grow goldens from real escalations, and gate PRs on the smoke suite.
3. Sampled online judging in Langfuse, with alerts.
4. Cost work (caching, routing), *after* the evals exist to show it didn't cost
   quality.
