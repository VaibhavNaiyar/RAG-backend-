"""Hybrid retrieval: BM25 + vectors -> Reciprocal Rank Fusion -> cross-encoder rerank.

Why each piece exists, in one line each:
  * BM25      — exact terms matter here: "2.4 mg", "BR-4", "MEN 2", "boxed warning".
  * vectors   — the user's words are not the corpus's ("can we say it works" ~ "efficacy claim").
  * RRF       — fuses two rankings without having to calibrate their raw scores.
  * rerank    — a cross-encoder reads query+passage together; fixes the top of the list.
  * margin    — keeps chunks close to the best one, so a strong answer is not padded with noise.
  * unscoped retry — "not in the doc I scoped to" is not "not in the corpus".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from rank_bm25 import BM25Okapi

from claimcheck import telemetry
from claimcheck.config import Settings
from claimcheck.embed import get_embedder, get_reranker
from claimcheck.schemas import Chunk, Hit

_TOK = re.compile(r"[a-z0-9]+(?:[.\-][a-z0-9]+)*")


def tokenize(s: str) -> list[str]:
    return _TOK.findall(s.lower())


@dataclass
class Index:
    chunks: list[Chunk]
    vectors: np.ndarray
    bm25: BM25Okapi
    meta: dict

    @property
    def doc_ids(self) -> list[str]:
        return sorted({c.doc_id for c in self.chunks})

    def catalogue(self) -> list[dict]:
        """What the planner sees about the corpus — the doc.list of this system."""
        seen: dict[str, dict] = {}
        for c in self.chunks:
            d = seen.setdefault(c.doc_id, {"doc_id": c.doc_id, "title": c.doc_title, "kind": c.kind, "chunks": 0})
            d["chunks"] += 1
        return list(seen.values())


@lru_cache(maxsize=2)
def load_index(index_dir: str) -> Index:
    from pathlib import Path

    d = Path(index_dir)
    if not (d / "chunks.jsonl").exists():
        raise FileNotFoundError(f"No index at {d}. Run: python -m claimcheck.cli ingest")
    chunks = [Chunk.model_validate_json(line) for line in (d / "chunks.jsonl").read_text().splitlines() if line]
    vectors = np.load(d / "vectors.npy")
    meta = json.loads((d / "meta.json").read_text())
    bm25 = BM25Okapi([tokenize(f"{c.section} {c.text}") for c in chunks])
    return Index(chunks=chunks, vectors=vectors, bm25=bm25, meta=meta)


class Retriever:
    def __init__(self, settings: Settings, index: Index | None = None) -> None:
        self.s = settings
        self.index = index or load_index(str(settings.index_dir))
        self.embedder = get_embedder(settings.embedder)
        if self.index.meta.get("embedder") != self.embedder.name:
            raise RuntimeError(
                f"Index built with {self.index.meta.get('embedder')!r} but CC_EMBEDDER gives "
                f"{self.embedder.name!r}. Re-run ingest."
            )
        self.reranker = get_reranker(settings.reranker)

    # -- single query ----------------------------------------------------------
    def _candidates(self, query: str, mask: np.ndarray) -> dict[int, dict]:
        k = self.s.candidate_k
        out: dict[int, dict] = {}
        if self.s.retrieval_mode in ("hybrid", "bm25"):
            scores = np.where(mask, self.index.bm25.get_scores(tokenize(query)), -np.inf)
            for rank, i in enumerate(np.argsort(-scores)[:k], start=1):
                if np.isfinite(scores[i]) and scores[i] > 0:
                    out.setdefault(int(i), {})["bm25"] = rank
        if self.s.retrieval_mode in ("hybrid", "vector"):
            q = self.embedder.embed([query], query=True)[0]
            sims = np.where(mask, self.index.vectors @ q, -np.inf)
            for rank, i in enumerate(np.argsort(-sims)[:k], start=1):
                if np.isfinite(sims[i]):
                    out.setdefault(int(i), {})["vec"] = rank
        return out

    def _fuse(self, per_query: list[dict[int, dict]]) -> dict[int, Hit]:
        """RRF across retrievers AND across sub-queries: score = Σ 1/(k + rank)."""
        k = self.s.rrf_k
        hits: dict[int, Hit] = {}
        for cands in per_query:
            for i, ranks in cands.items():
                h = hits.get(i) or Hit(chunk=self.index.chunks[i])
                for key, attr in (("bm25", "bm25_rank"), ("vec", "vector_rank")):
                    if key in ranks:
                        h.rrf += 1.0 / (k + ranks[key])
                        prev = getattr(h, attr)
                        setattr(h, attr, ranks[key] if prev is None else min(prev, ranks[key]))
                hits[i] = h
        return hits

    def _rerank(self, question: str, hits: list[Hit]) -> list[Hit]:
        if not hits:
            return hits
        if self.reranker is None:
            ordered = sorted(hits, key=lambda h: -h.rrf)
            for h in ordered:
                h.rerank = h.rrf / ordered[0].rrf
        else:
            raw = self.reranker.score(question, [f"{h.chunk.section}\n{h.chunk.text}" for h in hits])
            lo, hi = min(raw), max(raw)
            for h, r in zip(hits, raw, strict=True):
                h.rerank = (r - lo) / (hi - lo) if hi > lo else 1.0  # normalise to 0..1 for the margin
            ordered = sorted(hits, key=lambda h: -(h.rerank or 0))
        best = ordered[0].rerank or 0
        kept = [h for j, h in enumerate(ordered) if j < self.s.rerank_min_keep or (best - (h.rerank or 0)) <= self.s.rerank_margin]
        return kept[: self.s.top_k]

    # -- public ----------------------------------------------------------------
    def search(self, question: str, sub_queries: list[str], doc_ids: list[str] | None = None,
               allowed: list[str] | None = None) -> tuple[list[Hit], bool]:
        """Returns (hits, widened). `allowed` = docs the scope may widen into (the skill's sources)."""
        valid = [d for d in (doc_ids or []) if d in self.index.doc_ids]
        with telemetry.span("retrieve", question=question, sub_queries=sub_queries, doc_ids=valid,
                            mode=self.s.retrieval_mode, reranker=self.s.reranker) as sp:
            hits = self._search_once(question, sub_queries, valid)
            retried = False
            outside: list[str] = []
            if valid:
                # outside_scope: documents the planner did NOT scope to that rank in the
                # unscoped top 3 for this very query. The scope may have left out the
                # file holding the answer, so widen it rather than answer "not found".
                probe = self._fuse([self._candidates(q, np.ones(len(self.index.chunks), bool))
                                    for q in dict.fromkeys([*sub_queries, question])])
                top = sorted(probe.values(), key=lambda h: -h.rrf)[:3]
                # A sibling of a kind already in scope (zepbound_label when the planner
                # chose wegovy_label) was excluded deliberately: never widen into it.
                scoped_kinds = {c.kind for c in self.index.chunks if c.doc_id in valid}
                outside = sorted({h.chunk.doc_id for h in top if h.chunk.kind not in scoped_kinds
                                  and (not allowed or h.chunk.doc_id in allowed)} - set(valid))
                if outside or len(hits) < self.s.rerank_min_keep:
                    with telemetry.span("retrieve.widen_scope", outside_scope=outside):
                        hits = self._search_once(question, sub_queries, valid + outside if outside else [])
                        retried = True
            telemetry.set_attrs(sp, outside_scope=outside)
            for n, h in enumerate(hits, start=1):
                h.ref = f"E{n}"
            telemetry.set_attrs(
                sp,
                hits=[f"{h.ref}:{h.chunk.id}:{(h.rerank or 0):.2f}" for h in hits],
                retried_unscoped=retried,
            )
        telemetry.record_retrieval(hits[0].rerank or 0.0 if hits else 0.0, len(hits), retried)
        return hits, retried

    def _search_once(self, question: str, sub_queries: list[str], doc_ids: list[str]) -> list[Hit]:
        mask = np.array([not doc_ids or c.doc_id in doc_ids for c in self.index.chunks])
        queries = list(dict.fromkeys([*sub_queries, question]))  # original question always included
        fused = self._fuse([self._candidates(q, mask) for q in queries])
        pool = sorted(fused.values(), key=lambda h: -h.rrf)[: self.s.candidate_k]
        return self._rerank(question, pool)
