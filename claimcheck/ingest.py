"""Parse -> chunk -> embed -> write a local index.

    python -m claimcheck.cli ingest

Chunking is page-aware and section-aware because the citation has to point
somewhere a compliance reviewer can open: "Zepbound label — p.3, 5.1 Risk of
Thyroid C-Cell Tumors", never "chunk 47".

The index is three files (chunks.jsonl, vectors.npy, meta.json). That is the
right size for 5 documents. docs/SCALING.md covers the swap to a real store.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import time
from pathlib import Path

import numpy as np
import yaml

from claimcheck import telemetry
from claimcheck.config import ROOT, Settings
from claimcheck.embed import get_embedder
from claimcheck.schemas import Chunk

# Headings we recognise:  "5.1 Risk of Thyroid C-Cell Tumors", "## BR-3 No guaranteed results",
# "WARNING: RISK OF THYROID C-CELL TUMORS", "Part II.B Establishment claims"
_HEADING = re.compile(
    r"^\s*(?:#{1,4}\s+(?P<md>.+)|(?P<num>\d{1,2}(?:\.\d{1,2}){0,2})\s+(?P<numtitle>[A-Z][A-Za-z][^\n]{2,80})"
    r"|(?P<caps>[A-Z][A-Z0-9 ,:;/&()\-]{8,90}))\s*$"
)


def _clean(text: str) -> str:
    text = text.replace("­", "").replace("ﬁ", "fi").replace("ﬂ", "fl")
    text = re.sub(r"-\n(?=[a-z])", "", text)        # de-hyphenate line breaks
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def load_pages(path: Path) -> list[tuple[int, str]]:
    """[(page_number, text)]. HTML and Markdown are one 'page'."""
    if path.suffix == ".pdf":
        from pypdf import PdfReader

        return [(i + 1, _clean(p.extract_text() or "")) for i, p in enumerate(PdfReader(str(path)).pages)]
    raw = path.read_text(encoding="utf-8", errors="ignore")
    if path.suffix in {".html", ".htm"}:
        raw = re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", raw)
        raw = re.sub(r"(?i)<h([1-4])[^>]*>", lambda m: "\n" + "#" * int(m.group(1)) + " ", raw)
        raw = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>|<br\s*/?>", "\n", raw)
        raw = html.unescape(re.sub(r"<[^>]+>", " ", raw))
        raw = "\n".join(line.strip() for line in raw.splitlines())
    return [(1, _clean(raw))]


def _heading(line: str) -> str | None:
    m = _HEADING.match(line)
    if not m:
        return None
    if m.group("md"):
        return m.group("md").strip()
    if m.group("num"):
        return f"{m.group('num')} {m.group('numtitle').strip()}"
    caps = m.group("caps").strip()
    return caps.title() if len(caps.split()) >= 2 else None


def chunk_document(doc: dict, pages: list[tuple[int, str]], size: int, overlap: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    section = ""
    for page, text in pages:
        buf = ""
        n = 0

        def flush(force_section: str, page: int = page) -> None:
            nonlocal buf, n
            body = buf.strip()
            if len(body) > 40:
                chunks.append(
                    Chunk(
                        id=f"{doc['id']}:p{page}:{n}", doc_id=doc["id"], doc_title=doc["title"],
                        kind=doc["kind"], page=page, section=force_section, text=body,
                    )
                )
                n += 1
            buf = body[-overlap:] if len(body) > overlap else ""

        for para in re.split(r"\n\s*\n|\n(?=\s*(?:#|\d{1,2}\.\d))", text):
            first = para.strip().split("\n", 1)[0]
            h = _heading(first)
            if h and h != section:
                if buf.strip():
                    flush(section)
                    buf = ""  # no overlap across a section boundary
                section = h
            para = para.strip()
            if h:  # the heading lives in `section`; keep the body clean for citing
                para = para.split("\n", 1)[1].strip() if "\n" in para else ""
                if not para:
                    continue
            while len(buf) + len(para) > size:
                room = max(size - len(buf), 200)
                cut = para.rfind(". ", 0, room)
                cut = cut + 1 if cut > 100 else room
                buf += " " + para[:cut]
                para = para[cut:].strip()
                flush(section)
            buf += ("\n" if buf else "") + para
        flush(section)
    return chunks


def load_sources(settings: Settings) -> list[dict]:
    return yaml.safe_load(settings.sources_file.read_text())["documents"]


def build_index(settings: Settings) -> dict:
    t0 = time.perf_counter()
    docs, all_chunks, missing = load_sources(settings), [], []
    with telemetry.span("ingest", embedder=settings.embedder) as s:
        for doc in docs:
            path = ROOT / doc["file"]
            if not path.exists():
                missing.append(doc["file"])
                continue
            with telemetry.span("ingest.document", doc_id=doc["id"]):
                pages = load_pages(path)
                cs = chunk_document(doc, pages, settings.chunk_chars, settings.chunk_overlap)
                all_chunks.extend(cs)
                print(f"  {doc['id']:<22} {len(pages):>3} pages -> {len(cs):>4} chunks")
        if not all_chunks:
            raise SystemExit("No documents found. Run scripts/fetch_corpus.py first.")
        emb = get_embedder(settings.embedder)
        vecs = emb.embed([f"{c.section}\n{c.text}" for c in all_chunks])
        out = settings.index_dir
        out.mkdir(parents=True, exist_ok=True)
        with (out / "chunks.jsonl").open("w") as f:
            for c in all_chunks:
                f.write(c.model_dump_json() + "\n")
        np.save(out / "vectors.npy", vecs)
        digest = hashlib.sha256("".join(c.text for c in all_chunks).encode()).hexdigest()[:12]
        meta = {
            "embedder": emb.name, "dim": int(vecs.shape[1]), "chunks": len(all_chunks),
            "documents": sorted({c.doc_id for c in all_chunks}), "missing": missing,
            "corpus_sha": digest, "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "seconds": round(time.perf_counter() - t0, 2),
        }
        (out / "meta.json").write_text(json.dumps(meta, indent=2))
        telemetry.set_attrs(s, chunks=len(all_chunks), corpus_sha=digest)
    if missing:
        print(f"  missing (skipped): {missing}")
    return meta
