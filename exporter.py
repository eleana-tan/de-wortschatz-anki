"""Build Anki .apkg decks from unexported word_bank rows via genanki.

Usage (CLI)::

    python exporter.py                     # write under EXPORT_DIR, mark exported
    python exporter.py -o ./out.apkg       # explicit file path
    python exporter.py --db ./data/x.db --no-mark

HTTP: ``GET /export`` in ``main.py`` calls ``export_pending`` and returns the
``.apkg`` as a download. Empty queue → ``EmptyExportQueue`` → HTTP **404**.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import genanki

from database import connect, fetch_unexported, mark_exported

DEFAULT_DECK_NAME = os.getenv("ANKI_DECK_NAME", "German Wortschatz")
DEFAULT_MODEL_NAME = os.getenv("ANKI_MODEL_NAME", "German Wortschatz Card")
DEFAULT_EXPORT_DIR = os.getenv("EXPORT_DIR", "./exports")

# Stable IDs so re-imports merge into the same Anki deck / note type.
# Change only if you intentionally want a new deck identity.
_DECK_ID = 1_760_000_000_001
_MODEL_ID = 1_760_000_000_002

# Spec layouts (plus light CSS + FrontSide on the back for Anki flip UX):
#   Front: {{target_word}} + <small>{{grammar_details}}</small>
#   Back:  <b>{{definition}}</b> + <br><em>{{example_sentence}}</em>
FRONT_TEMPLATE = """
<div class="front">
  {{target_word}}
  <br>
  <small>{{grammar_details}}</small>
</div>
""".strip()

BACK_TEMPLATE = """
{{FrontSide}}
<hr id="answer">
<div class="back">
  <b>{{definition}}</b>
  <br>
  <em>{{example_sentence}}</em>
</div>
""".strip()

CARD_CSS = """
.card {
  font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
  font-size: 22px;
  text-align: center;
  color: #1a1a1a;
  background-color: #fafafa;
  padding: 1.2em;
}
.front { line-height: 1.35; }
.front small { color: #555; font-size: 0.85em; }
.back b { font-size: 1.15em; }
.back em { color: #333; font-size: 0.95em; display: inline-block; margin-top: 0.55em; }
"""


class EmptyExportQueue(ValueError):
    """Raised when word_bank has no rows with exported = 0.

    Mapped to HTTP 404 by ``GET /export`` — nothing to download yet.
    """

    def __init__(self, message: str = "No unexported words in word_bank") -> None:
        super().__init__(message)


def build_model(model_name: str = DEFAULT_MODEL_NAME) -> genanki.Model:
    return genanki.Model(
        _MODEL_ID,
        model_name,
        fields=[
            {"name": "target_word"},
            {"name": "grammar_details"},
            {"name": "definition"},
            {"name": "example_sentence"},
        ],
        templates=[
            {
                "name": "DE→EN",
                "qfmt": FRONT_TEMPLATE,
                "afmt": BACK_TEMPLATE,
            }
        ],
        css=CARD_CSS,
    )


def build_deck(deck_name: str = DEFAULT_DECK_NAME) -> genanki.Deck:
    return genanki.Deck(_DECK_ID, deck_name)


def note_from_row(model: genanki.Model, row: dict[str, Any]) -> genanki.Note:
    return genanki.Note(
        model=model,
        fields=[
            row.get("target_word") or "",
            row.get("grammar_details") or "",
            row.get("definition") or "",
            row.get("example_sentence") or "",
        ],
        guid=genanki.guid_for("wortschatz", row["id"], row.get("target_word") or ""),
    )


def _default_filename() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = random.randint(1000, 9999)
    return f"german-wortschatz-{stamp}-{suffix}.apkg"


def _resolve_output_path(
    *,
    export_dir: Optional[str] = None,
    output_path: Optional[str] = None,
) -> Path:
    """Return the .apkg path to write.

    - ``output_path`` ending in ``.apkg`` → that file
    - ``output_path`` as a directory → ``<dir>/<generated-name>.apkg``
    - otherwise → ``<export_dir>/<generated-name>.apkg``
    """
    if output_path:
        path = Path(output_path).expanduser().resolve()
        if path.suffix.lower() == ".apkg" or (not path.exists() and path.suffix):
            path.parent.mkdir(parents=True, exist_ok=True)
            return path
        path.mkdir(parents=True, exist_ok=True)
        return path / _default_filename()

    out_dir = Path(export_dir or DEFAULT_EXPORT_DIR).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / _default_filename()


def export_pending(
    *,
    db_path: Optional[str] = None,
    export_dir: Optional[str] = None,
    output_path: Optional[str] = None,
    deck_name: Optional[str] = None,
    mark_as_exported: bool = True,
) -> dict[str, Any]:
    """Export ``word_bank`` rows with ``exported = 0`` to an ``.apkg`` file.

    Builds the genanki package first, writes the file, then marks rows
    ``exported = 1`` only after a successful write (when ``mark_as_exported``).

    Returns metadata: path, filename, count, ids, deck_name, marked_exported.

    Raises:
        EmptyExportQueue: when there is nothing pending (HTTP layer → 404).
    """
    path = _resolve_output_path(export_dir=export_dir, output_path=output_path)
    resolved_deck = deck_name or DEFAULT_DECK_NAME

    conn = connect(db_path)
    try:
        rows = fetch_unexported(conn)
        if not rows:
            raise EmptyExportQueue()

        model = build_model()
        deck = build_deck(resolved_deck)
        for row in rows:
            deck.add_note(note_from_row(model, row))

        package = genanki.Package(deck)
        package.write_to_file(str(path))

        ids = [int(r["id"]) for r in rows]
        marked = False
        if mark_as_exported:
            mark_exported(conn, ids)
            conn.commit()
            marked = True
    finally:
        conn.close()

    return {
        "path": str(path),
        "filename": path.name,
        "count": len(ids),
        "ids": ids,
        "deck_name": resolved_deck,
        "marked_exported": marked,
    }


def _build_cli() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Export unexported word_bank rows to an Anki .apkg deck.",
    )
    p.add_argument(
        "--db",
        default=None,
        help="SQLite path (default: DATABASE_PATH / ./data/wortschatz.db)",
    )
    p.add_argument(
        "-o",
        "--output",
        default=None,
        help="Output .apkg file or directory (default: EXPORT_DIR)",
    )
    p.add_argument(
        "--deck",
        default=None,
        help=f"Anki deck name (default: {DEFAULT_DECK_NAME!r})",
    )
    p.add_argument(
        "--no-mark",
        action="store_true",
        help="Do not set exported=1 after writing the package",
    )
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_cli().parse_args(argv)
    try:
        result = export_pending(
            db_path=args.db,
            output_path=args.output,
            deck_name=args.deck,
            mark_as_exported=not args.no_mark,
        )
    except EmptyExportQueue as exc:
        print(f"empty: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - CLI surface
        print(f"export failed: {exc}", file=sys.stderr)
        return 1

    print(
        f"wrote {result['filename']} ({result['count']} notes) → {result['path']}"
        + (" [marked exported]" if result["marked_exported"] else " [not marked]")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
