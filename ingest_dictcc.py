#!/usr/bin/env python3
"""CLI: parse dict.cc German–English TSV into SQLite FTS5 (``dictcc_fts``).

dict.cc word-list format (tab-separated; ``#`` comment / license lines)::

    German term [tab] English term [tab] …

Gender tags appear in the German column as ``{m}``, ``{f}``, ``{n}``.
Bracket notes ``[…]`` and other ``{…}`` POS tags are stripped from the
searchable ``de_term`` / ``en_term`` while preserved in ``raw_de`` / ``raw_en``.

Idempotency
-----------
**Default is clear + rebuild** (``--replace``). Re-running the same dump does
not duplicate rows. Pass ``--append`` only when intentionally adding a second
file on top of an existing index (not recommended for a full dump reload).

The FTS schema matches ``database.lookup_dictcc_fts`` / ``lookup_dictcc_exact``
and the enricher (``de_term``, ``en_term``, ``gender``, ``pos``; lookups default
to ``de_only=True``).

Usage::

    python ingest_dictcc.py path/to/de-en.txt
    python ingest_dictcc.py path/to/de-en.txt --db ./data/wortschatz.db
    python ingest_dictcc.py path/to/de-en.txt --append   # keep existing rows
    python ingest_dictcc.py path/to/de-en.txt --encoding latin-1
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from pathlib import Path

from database import (
    DEFAULT_DB_PATH,
    clear_dictcc,
    connect,
    dictcc_count,
    init_db,
    insert_dictcc_entries,
    rebuild_dictcc_fts,
)

log = logging.getLogger("ingest_dictcc")

# Neuter/masculine/feminine gender tags from dict.cc.
GENDER_RE = re.compile(r"\{([mfn])\}", re.IGNORECASE)
# POS-like tags. Intentionally omits bare {m}/{f}/{n} — those are gender.
POS_RE = re.compile(
    r"\{(adj|adv|verb|v|pl|sg|pron|prep|conj|interj|num|abbr|art|"
    r"prefix|suffix|noun|vi|vt|vr)\}",
    re.IGNORECASE,
)
BRACKET_RE = re.compile(r"\[[^\]]*\]")
BRACE_TAG_RE = re.compile(r"\{[^}]+\}")
WHITESPACE_RE = re.compile(r"\s+")
# Leading articles sometimes appear in the German column; strip so exact
# lookup matches the enricher's lemma (article is rebuilt from gender).
ARTICLE_RE = re.compile(r"^(der|die|das|ein|eine)\s+", re.IGNORECASE)

# Encodings tried when --encoding is omitted (in order).
ENCODING_CANDIDATES = ("utf-8-sig", "utf-8", "latin-1", "cp1252")


def strip_annotations(text: str, *, strip_article: bool = False) -> str:
    """Remove ``[notes]`` and ``{tags}``; collapse whitespace.

    When ``strip_article`` is true (German column), also drop a leading
    definite/indefinite article so ``de_term`` is the bare lemma.
    """
    text = BRACKET_RE.sub(" ", text)
    text = BRACE_TAG_RE.sub(" ", text)
    text = WHITESPACE_RE.sub(" ", text).strip()
    if strip_article:
        text = ARTICLE_RE.sub("", text).strip()
    return text


def extract_gender(raw_de: str) -> str | None:
    match = GENDER_RE.search(raw_de)
    return match.group(1).lower() if match else None


def extract_pos(raw_de: str, raw_en: str) -> str | None:
    """Return first non-gender POS tag from either column."""
    # Strip gender braces so {n} is not confused with a POS tag.
    for blob in (raw_de, raw_en):
        cleaned = GENDER_RE.sub(" ", blob)
        match = POS_RE.search(cleaned)
        if match:
            return match.group(1).lower()
    return None


def parse_line(line: str) -> tuple[str, str, str | None, str | None, str, str] | None:
    """Parse one TSV line → ``(de_term, en_term, gender, pos, raw_de, raw_en)``.

    Skips blank lines, ``#`` comments, and rows without two non-empty columns.
    """
    # Normalize newlines / possible CR leftovers from Windows dumps.
    line = line.strip("\n\r")
    if not line or line.lstrip().startswith("#"):
        return None
    parts = line.split("\t")
    if len(parts) < 2:
        return None
    raw_de, raw_en = parts[0].strip(), parts[1].strip()
    if not raw_de or not raw_en:
        return None
    gender = extract_gender(raw_de)
    pos = extract_pos(raw_de, raw_en)
    de_term = strip_annotations(raw_de, strip_article=True)
    en_term = strip_annotations(raw_en)
    if not de_term or not en_term:
        return None
    return de_term, en_term, gender, pos, raw_de, raw_en


def detect_encoding(path: Path, preferred: str | None = None) -> str:
    """Return an encoding that can decode the file without errors.

    If ``preferred`` is set, that encoding is used as-is (caller's choice,
    including ``errors='replace'`` behavior at open time only when preferred
    fails strict decode — see ``open_tsv``).
    """
    if preferred:
        return preferred
    sample = path.read_bytes()[:65536]
    if not sample:
        return "utf-8-sig"
    for enc in ENCODING_CANDIDATES:
        try:
            sample.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "latin-1"


def open_tsv(path: Path, encoding: str | None):
    """Open the TSV with detected or explicit encoding."""
    enc = detect_encoding(path, encoding)
    # Preferred/explicit: replace bad bytes so a huge dump is not aborted.
    # Auto-detected: already validated strictly on a sample.
    errors = "replace" if encoding else "strict"
    log.info("Opening %s with encoding=%s", path, enc)
    return path.open("r", encoding=enc, errors=errors, newline="")


def ingest(
    tsv_path: Path,
    db_path: str,
    *,
    replace: bool = True,
    batch_size: int = 5000,
    encoding: str | None = None,
    progress_every: int = 50_000,
) -> dict[str, int]:
    """Load a dict.cc TSV into ``dictcc_entries`` / ``dictcc_fts``.

    Returns a stats dict: inserted, skipped, comments, total.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")

    init_db(db_path)
    inserted = 0
    skipped = 0
    comments = 0
    t0 = time.monotonic()

    with connect(db_path) as conn:
        # Speed knobs for bulk load (safe for a local rebuild).
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")

        if replace:
            log.info("Clearing existing dict.cc rows (replace mode)")
            clear_dictcc(conn)
            conn.commit()
        else:
            log.info("Appending to existing dict.cc rows (--append)")

        batch: list[tuple] = []
        last_progress_bucket = 0

        def flush(lineno: int = 0) -> None:
            nonlocal inserted, batch, last_progress_bucket
            if not batch:
                return
            insert_dictcc_entries(conn, batch)
            inserted += len(batch)
            conn.commit()
            batch = []
            if progress_every > 0 and inserted >= last_progress_bucket + progress_every:
                last_progress_bucket = (inserted // progress_every) * progress_every
                elapsed = time.monotonic() - t0
                rate = inserted / elapsed if elapsed > 0 else 0
                log.info(
                    "… %s rows (%.0f rows/s, line %s)",
                    f"{inserted:,}",
                    rate,
                    f"{lineno:,}" if lineno else "?",
                )

        lineno = 0
        with open_tsv(tsv_path, encoding) as fh:
            for lineno, line in enumerate(fh, start=1):
                stripped = line.strip("\n\r")
                if stripped.lstrip().startswith("#"):
                    comments += 1
                    continue
                if not stripped.strip():
                    skipped += 1
                    continue
                parsed = parse_line(line)
                if parsed is None:
                    skipped += 1
                    continue
                batch.append(parsed)
                if len(batch) >= batch_size:
                    flush(lineno)
            flush(lineno)

        # External-content FTS: rebuild once so the index matches content exactly.
        log.info("Rebuilding dictcc_fts index")
        rebuild_dictcc_fts(conn)
        conn.commit()
        total = dictcc_count(conn)

    elapsed = time.monotonic() - t0
    stats = {
        "inserted": inserted,
        "skipped": skipped,
        "comments": comments,
        "total": total,
    }
    log.info(
        "Done in %.1fs — inserted=%s skipped=%s comments=%s dictcc_entries=%s",
        elapsed,
        f"{inserted:,}",
        f"{skipped:,}",
        f"{comments:,}",
        f"{total:,}",
    )
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest dict.cc DE–EN TSV into SQLite FTS5 (dictcc_fts).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Idempotency: default clears and rebuilds the dict.cc tables so\n"
            "re-running the same dump is safe. Use --append to keep old rows.\n"
            "Do not commit dict.cc dumps or *.db files."
        ),
    )
    parser.add_argument(
        "tsv",
        type=Path,
        help="Path to dict.cc German–English word list (.txt/.tsv)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help=f"SQLite path (default: DATABASE_PATH or {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        default=True,
        help="Clear dict.cc rows before ingest (default; idempotent reload)",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="Keep existing rows and append (disables replace; may duplicate)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=5000,
        help="Commit every N rows (default: 5000)",
    )
    parser.add_argument(
        "--encoding",
        default=None,
        help="Text encoding (default: auto-detect utf-8-sig / utf-8 / latin-1 / cp1252)",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=50_000,
        help="Log progress every N inserted rows (0 disables; default: 50000)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Debug logging",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Warnings and errors only",
    )
    args = parser.parse_args(argv)

    level = logging.INFO
    if args.verbose:
        level = logging.DEBUG
    elif args.quiet:
        level = logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )

    if not args.tsv.is_file():
        log.error("File not found: %s", args.tsv)
        return 1

    db_path = args.db or DEFAULT_DB_PATH
    replace = not args.append
    log.info("Ingesting %s → %s (replace=%s)", args.tsv, db_path, replace)
    try:
        ingest(
            args.tsv,
            db_path,
            replace=replace,
            batch_size=args.batch_size,
            encoding=args.encoding,
            progress_every=args.progress_every,
        )
    except Exception as exc:
        log.error("Ingest failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
