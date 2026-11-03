"""Embedders and rerankers. Local by default: zero API credits for retrieval.

fastembed runs ONNX models on CPU (no torch):
  * BAAI/bge-small-en-v1.5           384-d embeddings, ~67 MB
  * Xenova/ms-marco-MiniLM-L-6-v2    cross-encoder reranker, ~80 MB

`hash` / `lexical` are dependency-free fallbacks for offline tests. They are
clearly worse — which is itself a useful eval ablation.
"""

from __future__ import annotations

import hashlib
import math
import re
from functools import lru_cache

import numpy as np

_W = re.compile(r"[a-z0-9]+")


def _norm(m: np.ndarray) -> np.ndarray:
    return m / np.clip(np.linalg.norm(m, axis=1, keepdims=True), 1e-9, None)


class HashEmbedder:
    name = "hash-512"
    dim = 512

    def embed(self, texts: list[str], *, query: bool = False) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            w = _W.findall(t.lower())
            for tok in w + [f"{a}_{b}" for a, b in zip(w, w[1:], strict=False)]:
                h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
                out[i, h % self.dim] += 1.0 if (h >> 9) & 1 else -1.0
        return _norm(out)


class FastEmbedder:
    name = "BAAI/bge-small-en-v1.5"

    def __init__(self) -> None:
        from fastembed import TextEmbedding

        self._m = TextEmbedding(self.name)
        self.dim = 384

    def embed(self, texts: list[str], *, query: bool = False) -> np.ndarray:
        # fastembed's default batch of 256 pads every text to the longest one; on CPU that ran ~10x
        # slower than small batches on the real corpus (128 chunks: 148 s at 128, 16 s at 16).
        it = self._m.query_embed(texts) if query else self._m.passage_embed(texts, batch_size=16)
        return _norm(np.array(list(it), dtype=np.float32))


class LexicalReranker:
    name = "lexical"

    def score(self, query: str, docs: list[str]) -> list[float]:
        q = set(_W.findall(query.lower()))
        return [len(q & set(_W.findall(d.lower()))) / math.sqrt(1 + len(q)) for d in docs]


class CrossEncoderReranker:
    name = "Xenova/ms-marco-MiniLM-L-6-v2"

    def __init__(self) -> None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        self._m = TextCrossEncoder(self.name)

    def score(self, query: str, docs: list[str]) -> list[float]:
        return [float(s) for s in self._m.rerank(query, docs)]


@lru_cache(maxsize=4)
def get_embedder(kind: str):
    return FastEmbedder() if kind == "fastembed" else HashEmbedder()


@lru_cache(maxsize=4)
def get_reranker(kind: str):
    if kind == "none":
        return None
    return CrossEncoderReranker() if kind == "cross" else LexicalReranker()
