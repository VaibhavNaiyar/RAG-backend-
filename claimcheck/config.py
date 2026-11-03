"""Every knob in one place. Env vars win; defaults are what the demo runs on."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parents[1]
PKG = Path(__file__).resolve().parent


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _flag(name: str, default: bool) -> bool:
    return _env(name, str(default)).lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # models
    llm_backend: str = field(default_factory=lambda: _env("CC_LLM", "litellm"))
    fast_model: str = field(default_factory=lambda: _env("CC_FAST_MODEL", ""))
    smart_model: str = field(default_factory=lambda: _env("CC_SMART_MODEL", ""))
    judge_model: str = field(default_factory=lambda: _env("CC_JUDGE_MODEL", ""))

    # retrieval
    embedder: str = field(default_factory=lambda: _env("CC_EMBEDDER", "fastembed"))
    reranker: str = field(default_factory=lambda: _env("CC_RERANKER", "cross"))
    retrieval_mode: str = field(default_factory=lambda: _env("CC_RETRIEVAL_MODE", "hybrid"))
    rrf_k: int = 60              # reciprocal rank fusion constant
    candidate_k: int = 30        # per-retriever candidates before fusion
    top_k: int = 6               # evidence chunks handed to the answerer
    rerank_margin: float = 0.35  # drop chunks scoring this far below the best (normalised 0..1)
    rerank_min_keep: int = 3
    chunk_chars: int = 900
    chunk_overlap: int = 150

    # pipeline
    use_verifier: bool = field(default_factory=lambda: _flag("CC_USE_VERIFIER", True))
    escalate_below: float = 0.6  # supported-claim fraction below which we escalate
    max_cost_usd: float = 0.05   # per-question budget guardrail
    answerer_extra: str = ""     # eval-only prompt suffix (regression simulation)

    # paths
    index_dir: Path = field(default_factory=lambda: ROOT / _env("CC_INDEX_DIR", "index"))
    sources_file: Path = field(default_factory=lambda: ROOT / _env("CC_SOURCES_FILE", "corpus/sources.yaml"))

    # observability
    prompts_from_langfuse: bool = field(default_factory=lambda: _flag("CC_PROMPTS_FROM_LANGFUSE", False))

    def with_(self, **kw) -> Settings:
        """Copy with overrides — how evals run ablations without touching env."""
        return replace(self, **kw)


def get_settings() -> Settings:
    return Settings()
