"""FastAPI app: capture → enrich → word_bank; export → Anki .apkg."""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from database import (
    connect,
    dictcc_count,
    init_db,
    insert_word_bank,
    list_word_bank,
    word_bank_stats,
)
from enricher import enrich_word
from exporter import EmptyExportQueue, export_pending

load_dotenv()

app = FastAPI(
    title="German Wortschatz → Anki Pipeline",
    description="Headless DE→EN vocabulary capture, hybrid enrichment, Anki export.",
    version="0.1.0",
)

# Allow local SPA (web/) and other browser clients; auth is Bearer, not cookies.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class CaptureRequest(BaseModel):
    input: str = Field(..., min_length=1, description="Raw German vocabulary capture")
    source: Optional[str] = Field(
        default=None,
        description="Capture source, e.g. ios_voice or ios_share",
    )


class CaptureResponse(BaseModel):
    ok: bool = True
    id: int
    target_word: str
    grammar_details: str
    definition: str
    example_sentence: str
    exported: int = 0


def _load_api_key() -> str:
    """Return configured SECURE_API_KEY, or raise if empty/unset."""
    key = (os.getenv("SECURE_API_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "SECURE_API_KEY is empty or unset. "
            "Set a non-empty secret in the environment (see .env.example) before starting the API."
        )
    return key


def require_bearer(authorization: Optional[str] = Header(default=None)) -> None:
    """Strict Authorization: Bearer <SECURE_API_KEY> for /capture, /export, /words."""
    expected = _load_api_key()
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header; expected: Bearer <SECURE_API_KEY>",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization[len("Bearer ") :].strip()
    # Empty token after "Bearer " is always unauthorized.
    # secrets.compare_digest is constant-time for equal-length strings;
    # unequal length raises ValueError on some Python versions → treat as mismatch.
    try:
        matched = bool(token) and secrets.compare_digest(token, expected)
    except (TypeError, ValueError):
        matched = False
    if not matched:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )


@app.on_event("startup")
def on_startup() -> None:
    # Refuse to serve if the capture/export key is missing — fail loud at boot.
    _load_api_key()
    init_db()


@app.get("/health")
def health() -> dict:
    db_path = os.getenv("DATABASE_PATH", "./data/wortschatz.db")
    try:
        with connect(db_path) as conn:
            stats = word_bank_stats(conn)
            fts_rows = dictcc_count(conn)
        db_ok = True
    except Exception as exc:  # pragma: no cover - defensive
        stats = {"total": 0, "pending_export": 0}
        fts_rows = 0
        db_ok = False
        return {
            "status": "degraded",
            "database": str(exc),
            "word_bank": stats,
            "dictcc_entries": fts_rows,
        }
    return {
        "status": "ok" if db_ok else "degraded",
        "database": db_path,
        "word_bank": stats,
        "dictcc_entries": fts_rows,
        "llm_provider": "gemini",
    }


@app.post("/capture", response_model=CaptureResponse, dependencies=[Depends(require_bearer)])
def capture(body: CaptureRequest) -> CaptureResponse:
    try:
        enrichment = enrich_word(body.input, source=body.source)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Enrichment failed: {exc}",
        ) from exc

    with connect() as conn:
        row_id = insert_word_bank(
            conn,
            raw_input=body.input.strip(),
            source=body.source,
            target_word=enrichment.target_word,
            grammar_details=enrichment.grammar_details,
            definition=enrichment.definition,
            example_sentence=enrichment.example_sentence,
        )
        conn.commit()

    return CaptureResponse(
        id=row_id,
        target_word=enrichment.target_word,
        grammar_details=enrichment.grammar_details,
        definition=enrichment.definition,
        example_sentence=enrichment.example_sentence,
        exported=0,
    )


@app.get("/words", dependencies=[Depends(require_bearer)])
def words() -> list[dict]:
    """Return all word_bank rows for flashcard review sync."""
    with connect() as conn:
        return list_word_bank(conn)


@app.get("/export", dependencies=[Depends(require_bearer)])
def export_deck() -> FileResponse:
    """Build an Anki ``.apkg`` from ``word_bank`` rows with ``exported = 0``.

    On success: downloadable ``.apkg``; those rows are marked ``exported = 1``.
    Empty queue: **404** with a clear JSON detail (nothing pending to export).
    Optional disk/CLI path: ``python exporter.py`` (see README).
    """
    try:
        result = export_pending(mark_as_exported=True)
    except EmptyExportQueue as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Export failed: {exc}",
        ) from exc

    path = Path(result["path"])
    if not path.is_file():
        raise HTTPException(status_code=500, detail="Export file missing after write")

    # octet-stream so browsers/Shortcuts always treat it as a file download
    return FileResponse(
        path=str(path),
        media_type="application/octet-stream",
        filename=result["filename"],
        headers={
            "X-Export-Count": str(result["count"]),
            "X-Deck-Name": result["deck_name"],
            "Content-Disposition": f'attachment; filename="{result["filename"]}"',
        },
    )


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host=host, port=port, reload=False)
