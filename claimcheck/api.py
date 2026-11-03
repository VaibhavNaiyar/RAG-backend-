"""HTTP surface: POST /api/ask, GET /api/pools, GET /api/health, and the UI at /."""

from __future__ import annotations

from functools import lru_cache

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from claimcheck import telemetry
from claimcheck.config import PKG, get_settings
from claimcheck.core import pools

telemetry.setup_telemetry("claimcheck-api")
app = FastAPI(title="ClaimCheck", version="0.1.0")


class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    mode: str = Field(default="pipeline", pattern="^(pipeline|deep)$")
    use_verifier: bool = True


@lru_cache(maxsize=4)
def _runner(mode: str, use_verifier: bool):
    s = get_settings().with_(use_verifier=use_verifier)
    if mode == "deep":
        from claimcheck.deep import DeepPipeline
        return DeepPipeline(s)
    from claimcheck.pipeline import Pipeline
    return Pipeline(s)


@app.get("/api/health")
def health() -> dict:
    s = get_settings()
    try:
        meta = _runner("pipeline", True).retriever.index.meta
    except FileNotFoundError as e:
        raise HTTPException(503, str(e)) from e
    return {"ok": True, "llm": s.llm_backend, "index": meta, "langfuse": bool(telemetry.langfuse())}


@app.get("/api/pools")
def list_pools() -> dict:
    s = get_settings()
    return {
        "agents": [{"name": a.name, "description": a.description, "model": pools.resolve_model(a.model, s) if a.model else None,
                    "response_format": a.response_format} for a in pools.agents().values()],
        "skills": [{"name": k.name, "description": k.description, "sources": k.extra.get("sources"),
                    "steps": [st.name for st in pools.steps_of(k)]} for k in pools.skills().values()],
    }


@app.post("/api/ask")
def ask(body: AskIn) -> dict:
    r = _runner(body.mode, body.use_verifier).run(body.question)
    telemetry.flush()
    out = r.model_dump()
    out["cost_usd"], out["latency_ms"] = r.cost_usd, r.latency_ms
    for h in out["hits"]:
        h["citation"] = f"{h['chunk']['doc_title']} — p.{h['chunk']['page']}" + (f", {h['chunk']['section']}" if h["chunk"]["section"] else "")
    return out


@app.get("/")
def ui() -> FileResponse:
    return FileResponse(PKG / "ui" / "index.html")
