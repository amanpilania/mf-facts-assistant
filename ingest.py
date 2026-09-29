"""Step 3 - Ingestion.

Reads data/sources.csv, fetches each source, extracts text, splits it into
chunks, tags every chunk with metadata, and builds the retrieval index.

Outputs:
  data/raw/<id>.txt       extracted text per source (for auditing)
  data/chunks.jsonl       one chunk per line with metadata
  data/embeddings.npy     normalised chunk embeddings (skipped with --no-embed)
  data/ingest_report.md   what was ingested, skipped, or needs attention

Manual fallback: if a page is JS-rendered or blocks scripts, save it from your
browser as data/manual/<id>.html or data/manual/<id>.pdf and re-run. Manual
files are used before fetching, and they also cover rows still marked TO_COLLECT.

Usage:
  python ingest.py              # fetch + chunk + embed
  python ingest.py --no-embed   # fetch + chunk only
"""
import argparse
import csv
import io
import json
import re
import sys
from datetime import date
from pathlib import Path

import pdfplumber
import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent))
from app.schemes import CSV_SCHEME_MAP, detect_schemes  # noqa: E402

DATA = Path(__file__).parent / "data"
MANUAL = DATA / "manual"
RAW = DATA / "raw"
EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

CHUNK_WORDS = 220      # small chunks keep the LLM input short
OVERLAP_WORDS = 40
MIN_TEXT_CHARS = 400   # below this, the page probably didn't render
HEADERS = {"User-Agent": "Mozilla/5.0 (facts-only-mf-assistant; educational project)"}


# ---------- extraction ----------

def pdf_pages(data: bytes) -> list[tuple[int, str]]:
    pages = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            pages.append((i, page.extract_text() or ""))
    return pages


def html_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "svg", "form"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def load_source(row: dict) -> tuple[list[tuple[int | None, str]], str]:
    """Return ([(page, text), ...], origin). page is None for HTML."""
    sid = row["id"]
    for ext in (".pdf", ".html", ".htm", ".txt"):
        f = MANUAL / f"{sid}{ext}"
        if f.exists():
            if ext == ".pdf":
                return pdf_pages(f.read_bytes()), f"manual:{f.name}"
            raw = f.read_text(encoding="utf-8", errors="ignore")
            text = raw if ext == ".txt" else html_text(raw)
            return [(None, text)], f"manual:{f.name}"

    url = row["url"].strip()
    if not url.startswith("http"):
        return [], "missing"
    resp = requests.get(url, headers=HEADERS, timeout=40)
    resp.raise_for_status()
    is_pdf = "pdf" in resp.headers.get("content-type", "").lower() or url.lower().endswith(".pdf")
    if is_pdf:
        return pdf_pages(resp.content), "fetched:pdf"
    return [(None, html_text(resp.text))], "fetched:html"


# ---------- chunking ----------

def split_words(text: str) -> list[str]:
    words = text.split()
    if len(words) <= CHUNK_WORDS:
        return [" ".join(words)] if words else []
    chunks, step = [], CHUNK_WORDS - OVERLAP_WORDS
    for start in range(0, len(words), step):
        piece = words[start:start + CHUNK_WORDS]
        if len(piece) < 30 and chunks:  # tiny tail: merge into previous chunk
            chunks[-1] += " " + " ".join(piece)
            break
        chunks.append(" ".join(piece))
        if start + CHUNK_WORDS >= len(words):
            break
    return chunks


AS_ON = re.compile(
    r"(?:as\s+on|as\s+of)\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4}|\d{1,2}[-/ ][A-Za-z]{3}[-/ ]\d{4})",
    re.I,
)


def build_chunks(row: dict, pages: list[tuple[int | None, str]], today: str) -> list[dict]:
    mode = CSV_SCHEME_MAP.get(row["scheme"].strip(), "general")
    topics = [t for t in row["topics"].split(";") if t]
    out = []
    for page, text in pages:
        # multi-scheme docs (e.g. complete factsheet): keep only in-scope pages
        page_schemes = detect_schemes(text) if mode == "detect" else None
        if mode == "detect" and not page_schemes:
            continue
        for piece in split_words(text):
            if mode == "detect":
                found = detect_schemes(piece) or page_schemes
                scheme = found[0] if len(found) == 1 else "multi"
                schemes = found
            else:
                scheme, schemes = mode, [mode]
            as_on = AS_ON.search(piece)
            out.append({
                "chunk_id": f"{row['id']}-{len(out):03d}",
                "source_id": row["id"],
                "publisher": row["publisher"],
                "title": row["title"],
                "url": row["url"],
                "scheme": scheme,
                "schemes": schemes,
                "topics": topics,
                "page": page,
                "source_as_on": as_on.group(1) if as_on else None,
                "retrieved_date": today,
                "text": piece,
            })
    return out


# ---------- embeddings ----------

def embed(chunks: list[dict]) -> None:
    import numpy as np
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(EMBED_MODEL)
    # prefix with title + scheme so short chunks still carry context
    texts = [f"{c['title']} | {c['scheme']} | {c['text']}" for c in chunks]
    vecs = model.encode(texts, batch_size=32, normalize_embeddings=True, show_progress_bar=True)
    np.save(DATA / "embeddings.npy", vecs.astype("float32"))


# ---------- main ----------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-embed", action="store_true")
    args = ap.parse_args()

    MANUAL.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()

    with open(DATA / "sources.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    all_chunks, report = [], []
    for row in rows:
        sid = row["id"]
        try:
            pages, origin = load_source(row)
        except Exception as e:  # network errors, 403s, bad PDFs
            report.append(f"| {sid} | FAILED | {type(e).__name__}: {e} |")
            continue
        if origin == "missing":
            report.append(f"| {sid} | SKIPPED | URL not collected yet |")
            continue

        full = "\n\n".join(t for _, t in pages)
        (RAW / f"{sid}.txt").write_text(full, encoding="utf-8")
        chunks = build_chunks(row, pages, today)
        all_chunks.extend(chunks)

        note = ""
        if len(full) < MIN_TEXT_CHARS:
            note = f"only {len(full)} chars - page may be JS-rendered; save it to data/manual/{sid}.html"
        elif not chunks:
            note = "no in-scope scheme mentions found"
        report.append(f"| {sid} | {origin} | {len(chunks)} chunks. {note} |")

    with open(DATA / "chunks.jsonl", "w", encoding="utf-8") as f:
        for c in all_chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    (DATA / "ingest_report.md").write_text(
        f"# Ingest report ({today})\n\nTotal chunks: {len(all_chunks)}\n\n"
        "| Source | Status | Detail |\n|---|---|---|\n" + "\n".join(report) + "\n",
        encoding="utf-8",
    )
    print("\n".join(report))
    print(f"\nTotal chunks: {len(all_chunks)}")

    if not args.no_embed and all_chunks:
        embed(all_chunks)
        print("Saved data/embeddings.npy")


if __name__ == "__main__":
    main()
