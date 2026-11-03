"""Download the demo corpus listed in corpus/sources.yaml.

    python scripts/fetch_corpus.py

Tries each URL in order and keeps the first response that looks like the right
file type. Anything that fails is reported with the manual fallback: download
it in a browser and save it at the listed `file` path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
UA = "Mozilla/5.0 (claimcheck demo corpus fetcher)"


def looks_right(path: Path, body: bytes) -> bool:
    if path.suffix == ".pdf":
        return body[:5] == b"%PDF-"
    if path.suffix in {".html", ".htm"}:
        return b"<html" in body[:5000].lower()
    return len(body) > 0


def main() -> int:
    sources = yaml.safe_load((ROOT / "corpus/sources.yaml").read_text(encoding="utf-8"))["documents"]
    failed = []
    with httpx.Client(follow_redirects=True, timeout=60, headers={"User-Agent": UA}) as http:
        for doc in sources:
            dest = ROOT / doc["file"]
            if dest.exists() and dest.stat().st_size > 0:
                print(f"  ok  {doc['id']:<22} already present")
                continue
            for url in doc.get("urls", []):
                try:
                    r = http.get(url)
                    if r.status_code == 200 and looks_right(dest, r.content):
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(r.content)
                        print(f"  ok  {doc['id']:<22} {len(r.content)//1024} KB  <- {url}")
                        break
                    print(f"  ..  {doc['id']:<22} HTTP {r.status_code} / wrong type  <- {url}")
                except httpx.HTTPError as e:
                    print(f"  ..  {doc['id']:<22} {type(e).__name__}  <- {url}")
            else:
                if doc.get("urls"):
                    failed.append(doc)
    for doc in failed:
        print(f"\nMANUAL: download {doc['urls'][0]}\n        save as {doc['file']}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
