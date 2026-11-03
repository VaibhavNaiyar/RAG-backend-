"""claimcheck CLI.

    python -m claimcheck.cli ingest
    python -m claimcheck.cli ask "Can we say 'clinically proven' for compounded semaglutide?"
    python -m claimcheck.cli ask --mode deep "..."
    python -m claimcheck.cli pools                 # agents + skills as the planner sees them
    python -m claimcheck.cli prompts push          # SKILL.md bodies -> Langfuse prompt management
    python -m claimcheck.cli serve                 # API + UI on :8000
"""

from __future__ import annotations

import argparse
import json
import sys

from claimcheck import telemetry
from claimcheck.config import get_settings


def _print_run(r) -> None:
    print(f"\nstatus   {r.status}    route {r.route.route if r.route else '-'}    "
          f"skill {r.plan.skill if r.plan else '-'}    docs {r.plan.doc_ids if r.plan else '-'}")
    if r.plan:
        print(f"queries  {r.plan.sub_queries}")
    if r.hits:
        print("evidence " + "  ".join(f"{h.ref}={h.chunk.doc_id} p{h.chunk.page} ({(h.rerank or 0):.2f})" for h in r.hits))
    if r.final:
        print(f"\nverdict  {r.final.verdict.upper()}\n{r.final.summary}\n")
        for c in r.final.claims:
            print(f"  • {c.text}  [{', '.join(c.citations)}]")
        if r.final.suggested_rewrite:
            print(f"\n  rewrite: {r.final.suggested_rewrite}")
    if r.verification and not r.verification.skipped:
        v = r.verification
        print(f"\nverifier supported={v.supported_fraction:.0%} removed={v.removed} rewritten={v.rewritten} "
              f"escalate={v.escalate} invalid_refs={v.invalid_citations}")
    if r.status != "answered":
        print(f"\n{r.message}")
    print(f"\ncost ${r.cost_usd:.5f}   llm latency {r.latency_ms:.0f} ms   trace {r.trace_id}")
    for u in r.usage:
        print(f"  {u.stage:<10} {u.model:<34} in={u.input_tokens:<6} out={u.output_tokens:<5} ${u.cost_usd:.5f} {u.latency_ms:.0f}ms")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="claimcheck")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ingest")
    a = sub.add_parser("ask")
    a.add_argument("question")
    a.add_argument("--mode", choices=["pipeline", "deep"], default="pipeline")
    a.add_argument("--no-verifier", action="store_true")
    a.add_argument("--json", action="store_true")
    sub.add_parser("pools")
    p = sub.add_parser("prompts")
    p.add_argument("action", choices=["push"])
    p.add_argument("--label", default="production")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)

    telemetry.setup_telemetry()
    settings = get_settings()

    if args.cmd == "ingest":
        from claimcheck.ingest import build_index
        print(json.dumps(build_index(settings), indent=2))

    elif args.cmd == "ask":
        if args.no_verifier:
            settings = settings.with_(use_verifier=False)
        if args.mode == "deep":
            from claimcheck.deep import DeepPipeline
            r = DeepPipeline(settings).run(args.question)
        else:
            from claimcheck.pipeline import Pipeline
            r = Pipeline(settings).run(args.question)
        print(r.model_dump_json(indent=2) if args.json else "", end="")
        if not args.json:
            _print_run(r)

    elif args.cmd == "pools":
        from claimcheck.core import pools
        print("AGENTS (agents/pool/*/agent.yaml + SKILL.md)")
        for ag in pools.agents().values():
            route = pools.resolve_model(ag.model, settings) if ag.model else "-"
            print(f"  {ag.name:<20} {route:<32} -> {ag.response_format:<15} {ag.description[:70]}")
        print("\nPLANNER SKILLS (skills/pool/*/SKILL.md) — Level 1, what the planner sees")
        for sk in pools.skills().values():
            steps = " → ".join(f"{st.name}" for st in pools.steps_of(sk))
            print(f"  {sk.name:<20} [{steps}]  sources={sk.extra.get('sources')}")
        problems = pools.validate_pools()
        print("\nvalidation: " + ("OK" if not problems else "\n  ".join(problems)))
        return 1 if problems else 0

    elif args.cmd == "prompts":
        lf = telemetry.langfuse()
        if not lf:
            print("Langfuse not configured (LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY).")
            return 1
        from claimcheck.core import pools
        for ag in pools.agents().values():
            lf.create_prompt(name=f"claimcheck-{ag.name}", prompt=ag.system_prompt, labels=[args.label], type="text",
                             config={"source": str(ag.path), "model_route": ag.model.name if ag.model else None})
            print(f"  pushed claimcheck-{ag.name} [{args.label}]")
        lf.flush()

    elif args.cmd == "serve":
        import uvicorn
        uvicorn.run("claimcheck.api:app", host="0.0.0.0", port=args.port)

    telemetry.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
