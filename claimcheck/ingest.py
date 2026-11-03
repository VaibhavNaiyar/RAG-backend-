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
from collections import Counter
from pathlib import Path

import numpy as np
import yaml

from claimcheck import telemetry
from claimcheck.config import ROOT, Settings
from claimcheck.embed import get_embedder
from claimcheck.schemas import Chunk

# Headings we recognise:  "5.1 Risk of Thyroid C-Cell Tumors", "4 CONTRAINDICATIONS", "## BR-3 No guaranteed
# results", "WARNING: RISK OF THYROID C-CELL TUMORS", "——— ADVERSE REACTIONS ———" (label Highlights),
# and the FTC guide's outline "III." > "B." > "4. THE TOTALITY OF THE EVIDENCE"
_HEADING = re.compile(
    r"^\s*(?:#{1,4}\s+(?P<md>.+)|(?P<num>\d{1,2}(?:\.\d{1,2}){0,2})\s+(?P<numtitle>[A-Z][A-Za-z][^\n]{2,120})"
    r"|[-—–]{3,}\s*(?P<hl>[A-Z][A-Z ,/&()]+?)\s*[-—–]{3,}"
    r"|(?P<outline>(?:[IVX]{1,4}|[A-H])\.\s+[A-Z][^\n]{2,90}|\d{1,2}\.\s+[A-Z][A-Z0-9 ,;:/&()'’\-]{8,90})"
    r"|(?P<named>Endnotes|References)|(?P<caps>[A-Z][A-Z0-9 ,:;/&()\-]{8,90}))\s*$"
)
# Lines that start a new paragraph even without a blank line before them (pypdf rarely emits one).
_PARA = re.compile(
    r"\n\s*\n|\n(?=\s*(?:#|\d{1,2}\.\d|\d{1,2}\s+[A-Z]{3}[A-Z ,/&()\-]*\s*(?:\n|$)|[-—–]{3,}"
    r"|(?:[IVX]{1,4}|[A-H])\.\s+[A-Z]|\d{1,2}\.\s+[A-Z]{3}[A-Z ,;:/&()'’\-]*\s*(?:\n|$)"
    r"|(?:Endnotes|References)\s*(?:\n|$)"
    r"|WARNING:|FULL PRESCRIBING INFORMATION|HIGHLIGHTS OF|MEDICATION GUIDE|INSTRUCTIONS FOR USE))"
)
_OUTLINE = re.compile(r"(?:(?P<roman>[IVX]{1,4})|(?P<letter>[A-H])|(?P<n>\d{1,2}))\.\s+(?P<title>.+)")
_WRAPPED = re.compile(r"\b(?:and|or|of|in|with|for|to|the|a|an|between|during|from)$", re.I)
_ACRONYMS = {"FDA", "FTC", "DSHEA", "OTC"}
_PATIENT_PART = re.compile(r"(Medication Guide|Instructions For Use)\s*\d*\s*$", re.I)


def _clean(text: str) -> str:
    text = text.replace("­", "").replace("ﬁ", "fi").replace("ﬂ", "fl").replace("−", "-").replace("\xa0", " ")
    text = re.sub(r"-\n(?=[a-z])", "", text)        # de-hyphenate line breaks
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def load_pages(path: Path) -> list[tuple[int, str]]:
    """[(page_number, text)]. HTML and Markdown are one 'page'."""
    if path.suffix == ".pdf":
        from pypdf import PdfReader

        pages = [(i + 1, _clean(_page_text(p))) for i, p in enumerate(PdfReader(str(path)).pages)]
        return _drop_contents(_strip_running(pages))
    raw = path.read_text(encoding="utf-8", errors="ignore")
    if path.suffix in {".html", ".htm"}:
        raw = re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", raw)
        raw = re.sub(r"(?i)<h([1-4])[^>]*>", lambda m: "\n" + "#" * int(m.group(1)) + " ", raw)
        raw = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>|<br\s*/?>", "\n", raw)
        raw = html.unescape(re.sub(r"<[^>]+>", " ", raw))
        raw = "\n".join(line.strip() for line in raw.splitlines())
    return [(1, _clean(raw))]


def _page_text(page) -> str:
    """pypdf text, but a two-column page is read left column first.

    pypdf follows the PDF's drawing order, which on some label pages is the right column first, so
    text lands under the wrong heading. A line belongs to the column it starts in; on the real
    labels one-column pages put <0.2 of their text in right-starting lines, two-column pages 0.27-0.63.
    """
    frags: list[tuple[float, str]] = []
    text = page.extract_text(visitor_text=lambda t, cm, tm, fd, fs: frags.append((tm[4] * cm[0] + cm[4], t)))
    mid, cols, col, pending, line_start = float(page.mediabox.width) / 2, ([], []), 0, "", True
    for fx, t in frags:
        if not t.strip():  # whitespace and newlines belong to the line that follows them
            pending += t
            line_start = line_start or "\n" in t
            continue
        if line_start:
            col = int(fx >= mid)
        cols[col].append(pending + t)
        pending, line_start = "", t.endswith("\n")
    cols[col].append(pending)
    if 0.25 <= sum(map(len, cols[1])) / max(1, len(text)) <= 0.75:
        return "".join(cols[0]) + "\n\n" + "".join(cols[1])
    return text or ""


def _strip_running(pages: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Drop page numbers and running headers/footers, which would otherwise open a chunk on every page.

    A label's Medication Guide / Instructions for Use pages announce themselves only in their running
    header, so that header becomes a heading instead of being dropped.
    """
    def key(line: str) -> str:
        return re.sub(r"\d+", "#", line.strip())

    def edges(lines: list[str]) -> list[int]:
        return list(range(len(lines))) if len(lines) <= 6 else [*range(3), *range(len(lines) - 3, len(lines))]

    seen = Counter(k for _, t in pages for k in {key(ln) for ln in (t.split("\n")[i] for i in edges(t.split("\n")))})
    running = {k for k, n in seen.items() if k and n >= max(3, len(pages) // 3)}
    out = []
    for page, text in pages:
        lines = text.split("\n")
        for i in edges(lines):
            if i < 3 and (m := _PATIENT_PART.search(lines[i])):
                lines[i] = m.group(1).upper()
            elif key(lines[i]) in running or re.fullmatch(r"\s*\d{1,3}\s*", lines[i]):
                lines[i] = ""
        out.append((page, _clean("\n".join(lines))))
    return out


def _drop_contents(pages: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """A label's table of contents repeats every heading; indexed, it turns into bogus one-line sections."""
    start, end = "FULL PRESCRIBING INFORMATION: CONTENTS", "FULL PRESCRIBING INFORMATION"
    out, skipping = [], False
    for page, text in pages:
        if skipping:
            j = text.find(end)
            text, skipping = (text[j:], False) if j >= 0 else ("", True)
        if (i := text.find(start)) >= 0:
            j = text.find(end, i + len(start))
            text, skipping = (text[:i] + text[j:], False) if j >= 0 else (text[:i], True)
        out.append((page, text))
    return out


def _heading(line: str) -> str | None:
    m = _HEADING.match(line)
    if not m:
        return None
    if m.group("md"):
        return m.group("md").strip()
    if m.group("num"):
        title = m.group("numtitle").strip()
        if "." not in m.group("num") and len(title) > 80:  # long undotted lines are footnotes, not sections
            return None
        return f"{m.group('num')} {title.title() if title.isupper() else title}"
    if m.group("hl"):
        return f"Highlights: {m.group('hl').strip().title()}"
    if m.group("outline"):
        return m.group("outline").strip()
    if m.group("named"):
        return m.group("named")
    caps = m.group("caps").strip()
    return caps.title() if len(caps.split()) >= 2 else None


def _nest(heading: str, trail: list[str]) -> tuple[str, list[str]]:
    """FTC outline levels "III." > "B." > "4." become one citable label: "III.B.4. Title"."""
    m = _OUTLINE.fullmatch(heading)
    if not m:
        return heading, trail
    depth = 0 if m.group("roman") else 1 if m.group("letter") else 2
    trail = (trail + ["?"] * 3)[:depth] + [m.group("roman") or m.group("letter") or m.group("n")]
    title = m.group("title")
    if title.isupper():
        title = " ".join(w if w.strip("(),") in _ACRONYMS else w.lower() for w in title.split())
        title = title[0].upper() + title[1:]
    return f"{'.'.join(trail)}. {title}", trail


def chunk_document(doc: dict, pages: list[tuple[int, str]], size: int, overlap: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    section, trail = "", []
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

        for para in _PARA.split(text):
            lines = para.strip().split("\n")
            first, used = lines[0].strip(), 1
            if len(lines) > 1 and _WRAPPED.search(first) and _heading(f"{first} {lines[1].strip()}"):
                first, used = f"{first} {lines[1].strip()}", 2  # heading wrapped onto a second line
            h = _heading(first)
            if h:
                h, trail = _nest(h, trail)
            if h and h != section:
                if buf.strip():
                    flush(section)
                    buf = ""  # no overlap across a section boundary
                section = h
            para = para.strip()
            if h:  # the heading lives in `section`; keep the body clean for citing
                para = "\n".join(lines[used:]).strip()
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
    return yaml.safe_load(settings.sources_file.read_text(encoding="utf-8"))["documents"]


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
        with (out / "chunks.jsonl").open("w", encoding="utf-8") as f:
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
        (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        telemetry.set_attrs(s, chunks=len(all_chunks), corpus_sha=digest)
    if missing:
        print(f"  missing (skipped): {missing}")
    return meta
