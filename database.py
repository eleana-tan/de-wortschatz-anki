"""SQLite helpers for word_bank and dict.cc FTS5."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

DEFAULT_DB_PATH = os.getenv("DATABASE_PATH", "./data/wortschatz.db")


def get_db_path(db_path: Optional[str] = None) -> Path:
    return Path(db_path or DEFAULT_DB_PATH).expanduser().resolve()


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = get_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db_session(db_path: Optional[str] = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Optional[str] = None) -> Path:
    """Create word_bank and dictcc_fts tables if missing."""
    path = get_db_path(db_path)
    with db_session(str(path)) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS word_bank (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raw_input TEXT NOT NULL,
                source TEXT,
                target_word TEXT NOT NULL,
                grammar_details TEXT,
                definition TEXT NOT NULL,
                example_sentence TEXT,
                exported INTEGER NOT NULL DEFAULT 0 CHECK (exported IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_word_bank_exported
                ON word_bank(exported);

            CREATE INDEX IF NOT EXISTS idx_word_bank_target
                ON word_bank(target_word);

            -- External-content FTS5 over a content table so we can store gender tags.
            CREATE TABLE IF NOT EXISTS dictcc_entries (
                rowid INTEGER PRIMARY KEY,
                de_term TEXT NOT NULL,
                en_term TEXT NOT NULL,
                gender TEXT,
                pos TEXT,
                raw_de TEXT,
                raw_en TEXT
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS dictcc_fts USING fts5(
                de_term,
                en_term,
                gender UNINDEXED,
                pos UNINDEXED,
                content='dictcc_entries',
                content_rowid='rowid',
                tokenize='unicode61 remove_diacritics 2'
            );

            CREATE TRIGGER IF NOT EXISTS dictcc_entries_ai AFTER INSERT ON dictcc_entries BEGIN
                INSERT INTO dictcc_fts(rowid, de_term, en_term, gender, pos)
                VALUES (new.rowid, new.de_term, new.en_term, new.gender, new.pos);
            END;

            CREATE TRIGGER IF NOT EXISTS dictcc_entries_ad AFTER DELETE ON dictcc_entries BEGIN
                INSERT INTO dictcc_fts(dictcc_fts, rowid, de_term, en_term, gender, pos)
                VALUES ('delete', old.rowid, old.de_term, old.en_term, old.gender, old.pos);
            END;

            CREATE TRIGGER IF NOT EXISTS dictcc_entries_au AFTER UPDATE ON dictcc_entries BEGIN
                INSERT INTO dictcc_fts(dictcc_fts, rowid, de_term, en_term, gender, pos)
                VALUES ('delete', old.rowid, old.de_term, old.en_term, old.gender, old.pos);
                INSERT INTO dictcc_fts(rowid, de_term, en_term, gender, pos)
                VALUES (new.rowid, new.de_term, new.en_term, new.gender, new.pos);
            END;
            """
        )
    return path


def clear_dictcc(conn: sqlite3.Connection) -> None:
    """Remove all dict.cc rows. Deletes content rows; triggers keep FTS5 in sync.

    For a full dump reload prefer this (or ``rebuild_dictcc_fts`` after bulk
    load) over deleting the FTS table directly — external-content FTS5 expects
    the content table to be the source of truth.
    """
    conn.execute("DELETE FROM dictcc_entries")
    # Ensure the external-content index is empty even if triggers were missing.
    try:
        conn.execute("INSERT INTO dictcc_fts(dictcc_fts) VALUES('delete-all')")
    except sqlite3.OperationalError:
        conn.execute("DELETE FROM dictcc_fts")


def rebuild_dictcc_fts(conn: sqlite3.Connection) -> None:
    """Rebuild the external-content FTS5 index from ``dictcc_entries``."""
    conn.execute("INSERT INTO dictcc_fts(dictcc_fts) VALUES('rebuild')")


def insert_dictcc_entry(
    conn: sqlite3.Connection,
    *,
    de_term: str,
    en_term: str,
    gender: Optional[str] = None,
    pos: Optional[str] = None,
    raw_de: Optional[str] = None,
    raw_en: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO dictcc_entries (de_term, en_term, gender, pos, raw_de, raw_en)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (de_term, en_term, gender, pos, raw_de, raw_en),
    )


def insert_dictcc_entries(
    conn: sqlite3.Connection,
    rows: list[tuple[str, str, Optional[str], Optional[str], Optional[str], Optional[str]]],
) -> int:
    """Bulk-insert ``(de_term, en_term, gender, pos, raw_de, raw_en)`` rows.

    AFTER INSERT triggers keep ``dictcc_fts`` in sync. Returns the number of
    rows inserted.
    """
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO dictcc_entries (de_term, en_term, gender, pos, raw_de, raw_en)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return len(rows)


def lookup_dictcc_exact(conn: sqlite3.Connection, term: str, limit: int = 20) -> list[dict[str, Any]]:
    """Exact (case-insensitive) match on German headword."""
    cleaned = term.strip()
    if not cleaned:
        return []
    rows = conn.execute(
        """
        SELECT de_term, en_term, gender, pos, raw_de, raw_en
        FROM dictcc_entries
        WHERE lower(de_term) = lower(?)
        LIMIT ?
        """,
        (cleaned, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def lookup_dictcc_fts(
    conn: sqlite3.Connection,
    term: str,
    limit: int = 20,
    *,
    de_only: bool = True,
) -> list[dict[str, Any]]:
    """FTS5 match for DE→EN lookup. Defaults to ``de_term`` column only.

    Falls back to a prefix query when the phrase match is empty. Set
    ``de_only=False`` to search both German and English columns.
    """
    cleaned = term.strip()
    if not cleaned:
        return []

    # Quote for FTS5 phrase; escape embedded double quotes.
    phrase = '"' + cleaned.replace('"', '""') + '"'
    match_expr = f"de_term:{phrase}" if de_only else phrase
    rows = conn.execute(
        """
        SELECT de_term, en_term, gender, pos
        FROM dictcc_fts
        WHERE dictcc_fts MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (match_expr, limit),
    ).fetchall()
    if rows:
        return [dict(r) for r in rows]

    prefix_token = cleaned.replace('"', "") + "*"
    prefix_expr = f"de_term:{prefix_token}" if de_only else prefix_token
    try:
        rows = conn.execute(
            """
            SELECT de_term, en_term, gender, pos
            FROM dictcc_fts
            WHERE dictcc_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (prefix_expr, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]


def insert_word_bank(
    conn: sqlite3.Connection,
    *,
    raw_input: str,
    source: Optional[str],
    target_word: str,
    grammar_details: Optional[str],
    definition: str,
    example_sentence: Optional[str],
) -> int:
    cur = conn.execute(
        """
        INSERT INTO word_bank (
            raw_input, source, target_word, grammar_details,
            definition, example_sentence, exported
        ) VALUES (?, ?, ?, ?, ?, ?, 0)
        """,
        (
            raw_input,
            source,
            target_word,
            grammar_details,
            definition,
            example_sentence,
        ),
    )
    return int(cur.lastrowid)


def fetch_unexported(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, raw_input, source, target_word, grammar_details,
               definition, example_sentence, exported, created_at
        FROM word_bank
        WHERE exported = 0
        ORDER BY id ASC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def list_word_bank(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """All word_bank rows for review sync (JSON-friendly plain dicts)."""
    rows = conn.execute(
        """
        SELECT id, raw_input, source, target_word, grammar_details,
               definition, example_sentence, exported, created_at, updated_at
        FROM word_bank
        ORDER BY id ASC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def mark_exported(conn: sqlite3.Connection, ids: list[int]) -> int:
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    cur = conn.execute(
        f"""
        UPDATE word_bank
        SET exported = 1, updated_at = datetime('now')
        WHERE id IN ({placeholders}) AND exported = 0
        """,
        ids,
    )
    return int(cur.rowcount)


def dictcc_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM dictcc_entries").fetchone()
    return int(row["n"] if row else 0)


def word_bank_stats(conn: sqlite3.Connection) -> dict[str, int]:
    total = conn.execute("SELECT COUNT(*) AS n FROM word_bank").fetchone()["n"]
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM word_bank WHERE exported = 0"
    ).fetchone()["n"]
    return {"total": int(total), "pending_export": int(pending)}
