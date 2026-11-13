"""Hybrid enrichment: local dict.cc FTS5 first, then LLM for spelling / plural / example.

Lookup order (DE→EN):
1. Normalize input (strip articles / {m}/{f}/{n} tags).
2. Exact case-insensitive match on German headword in SQLite.
3. FTS5 match scoped to ``de_term`` (prefix fallback).
4. If LLM API key is configured: phonetic cleanup when ``source == ios_voice``
   or there was no exact hit; always ask for missing plural + one German example
   as structured JSON.
5. If no API key (or LLM call fails): return dict.cc-based enrichment when
   available; otherwise a minimal offline stub so capture still succeeds.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from typing import Any, Optional

from database import connect, lookup_dictcc_exact, lookup_dictcc_fts

logger = logging.getLogger(__name__)

GENDER_ARTICLE = {"m": "der", "f": "die", "n": "das"}
GENDER_LABEL = {"m": "Masculine", "f": "Feminine", "n": "Neuter"}
ARTICLE_RE = re.compile(r"^(der|die|das)\s+", re.IGNORECASE)
BRACE_TAG_RE = re.compile(r"\{[^}]+\}")
WHITESPACE_RE = re.compile(r"\s+")
OFFLINE_DEFINITION = "(no dict.cc match; LLM unavailable)"

SYSTEM_PROMPT = """You are a careful German↔English lexicographer.
Return ONLY valid JSON with these keys:
  target_word: German headword with definite article when a noun (e.g. "die Veranstaltung")
  grammar_details: short POS/gender/plural line (e.g. "Feminine • Pl: Veranstaltungen")
  definition: concise English gloss(es), semicolon-separated if multiple
  example_sentence: one natural German example that uses the target word
Rules:
- Prefer Standard German.
- If the input may be misspelled (voice dictation), correct to the intended lemma.
- If gender/plural unknown, give your best standard form and note uncertainty briefly in grammar_details.
- Do not wrap JSON in markdown fences.
"""


@dataclass
class Enrichment:
    target_word: str
    grammar_details: str
    definition: str
    example_sentence: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def _normalize_lookup_term(raw: str) -> str:
    """Strip leading articles and brace tags so FTS/exact hit the dict.cc lemma."""
    text = BRACE_TAG_RE.sub(" ", (raw or "").strip())
    text = ARTICLE_RE.sub("", text)
    return WHITESPACE_RE.sub(" ", text).strip()


def _unique_definitions(hits: list[dict[str, Any]], limit: int = 6) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for h in hits:
        en = (h.get("en_term") or "").strip()
        if not en:
            continue
        key = en.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(en)
        if len(out) >= limit:
            break
    return out


def _majority_gender(hits: list[dict[str, Any]]) -> Optional[str]:
    """Majority vote over dict.cc gender tags {m}/{f}/{n} stored as m/f/n."""
    counts: dict[str, int] = {}
    for h in hits:
        g = (h.get("gender") or "").lower()
        if g in GENDER_ARTICLE:
            counts[g] = counts.get(g, 0) + 1
    if not counts:
        return None
    return max(counts, key=counts.get)


def _lemma_from_hits(raw: str, hits: list[dict[str, Any]]) -> str:
    if not hits:
        return raw.strip()
    counts: dict[str, int] = {}
    for h in hits:
        de = (h.get("de_term") or "").strip()
        if de:
            counts[de] = counts.get(de, 0) + 1
    if not counts:
        return raw.strip()
    return max(counts, key=counts.get)


def _build_from_dictcc(raw_input: str, hits: list[dict[str, Any]]) -> Enrichment:
    """Build enrichment fields from local dict.cc rows (gender tags → article)."""
    lemma = _lemma_from_hits(raw_input, hits)
    gender = _majority_gender(hits)
    article = GENDER_ARTICLE.get(gender or "", "")
    target = f"{article} {lemma}".strip() if article else lemma
    defs = _unique_definitions(hits)
    definition = "; ".join(defs) if defs else ""
    if gender:
        # Plural is filled by the LLM when available; offline leaves it out.
        grammar = GENDER_LABEL[gender]
    else:
        pos = (hits[0].get("pos") or "").strip() if hits else ""
        grammar = pos.upper() if pos else "See enrichment"
    return Enrichment(
        target_word=target,
        grammar_details=grammar,
        definition=definition,
        example_sentence="",
    )


def _offline_stub(raw_input: str) -> Enrichment:
    """Minimal capture payload when neither dict.cc nor LLM can enrich."""
    lemma = _normalize_lookup_term(raw_input) or raw_input.strip()
    return Enrichment(
        target_word=lemma,
        grammar_details="Unavailable offline",
        definition=OFFLINE_DEFINITION,
        example_sentence="",
    )


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    return json.loads(text)


def llm_configured() -> bool:
    """True when GOOGLE_API_KEY is set (Gemini via google-genai)."""
    return bool((os.getenv("GOOGLE_API_KEY") or "").strip())


def _llm_enrich(
    raw_input: str,
    *,
    source: Optional[str],
    dict_hints: list[dict[str, Any]],
    need_spelling: bool,
) -> Enrichment:
    hints = [
        {
            "de": h.get("de_term"),
            "en": h.get("en_term"),
            "gender": h.get("gender"),
            "pos": h.get("pos"),
        }
        for h in dict_hints[:12]
    ]
    user_payload = {
        "input": raw_input,
        "source": source,
        "need_spelling_correction": need_spelling,
        "dictcc_hints": hints,
        "direction": "DE→EN",
    }
    user_msg = (
        "Enrich this German vocabulary capture into the required JSON schema.\n"
        + json.dumps(user_payload, ensure_ascii=False, indent=2)
    )
    data = _call_gemini(user_msg)

    return Enrichment(
        target_word=str(data.get("target_word") or raw_input).strip(),
        grammar_details=str(data.get("grammar_details") or "").strip(),
        definition=str(data.get("definition") or "").strip(),
        example_sentence=str(data.get("example_sentence") or "").strip(),
    )


def _call_gemini(user_msg: str) -> dict[str, Any]:
    api_key = (os.getenv("GOOGLE_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY is not set")

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    model = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
    response = client.models.generate_content(
        model=model,
        contents=user_msg,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.2,
            response_mime_type="application/json",
        ),
    )
    text = getattr(response, "text", None) or ""
    if not text and getattr(response, "candidates", None):
        parts = response.candidates[0].content.parts
        text = "".join(getattr(p, "text", "") or "" for p in parts)
    return _extract_json(text)


def _needs_spelling_llm(source: Optional[str], exact_hits: list[dict[str, Any]]) -> bool:
    """Phonetic cleanup when voice dictation or no exact local match."""
    if (source or "").lower() == "ios_voice":
        return True
    return len(exact_hits) == 0


def _needs_completion(base: Optional[Enrichment]) -> bool:
    """True when plural line or example sentence is still missing."""
    if base is None or not base.definition:
        return True
    if not (base.example_sentence or "").strip():
        return True
    if "Pl:" not in (base.grammar_details or ""):
        return True
    return False


def _merge_llm_over_dictcc(
    llm: Enrichment,
    base: Optional[Enrichment],
    *,
    need_spelling: bool,
) -> Enrichment:
    """Prefer LLM structure; keep solid dict.cc gloss / headword when LLM blanks them."""
    if base and base.definition and not llm.definition:
        llm.definition = base.definition
    if base and base.target_word and not need_spelling:
        # Keep article+lemma from local gender tags unless we asked for spelling fix.
        if not llm.target_word or llm.target_word == llm.definition:
            llm.target_word = base.target_word
    if base and base.grammar_details and not llm.grammar_details:
        llm.grammar_details = base.grammar_details
    return llm


def enrich_word(
    raw_input: str,
    *,
    source: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Enrichment:
    """Hybrid lookup: FTS5/exact first, LLM for spelling, plural, example.

    Without an LLM API key, returns dict.cc-only fields (gender from {m}/{f}/{n},
    English glosses) and leaves ``example_sentence`` empty; plural is omitted from
    ``grammar_details``. If dict.cc also misses, returns an offline stub instead of
    raising — capture can still write ``word_bank``.
    """
    cleaned = (raw_input or "").strip()
    if not cleaned:
        raise ValueError("input must be a non-empty string")

    lookup_term = _normalize_lookup_term(cleaned) or cleaned

    with connect(db_path) as conn:
        exact = lookup_dictcc_exact(conn, lookup_term)
        # Also try the raw string in case the lemma already matched with article kept.
        if not exact and lookup_term != cleaned:
            exact = lookup_dictcc_exact(conn, cleaned)
        fts = exact or lookup_dictcc_fts(conn, lookup_term, de_only=True)
        if not fts and lookup_term != cleaned:
            fts = lookup_dictcc_fts(conn, cleaned, de_only=True)

    need_spelling = _needs_spelling_llm(source, exact)
    base = _build_from_dictcc(lookup_term, fts) if fts else None

    want_llm = need_spelling or _needs_completion(base)
    if want_llm and llm_configured():
        try:
            llm = _llm_enrich(
                cleaned,
                source=source,
                dict_hints=fts,
                need_spelling=need_spelling,
            )
            return _merge_llm_over_dictcc(llm, base, need_spelling=need_spelling)
        except Exception as exc:
            logger.warning("LLM enrichment failed; using offline/dict.cc fallback: %s", exc)
            if base and base.definition:
                return base
            return _offline_stub(cleaned)

    # No API key (or LLM not needed): graceful offline path.
    if base and base.definition:
        return base
    if base:
        # Hits without English gloss — still better than a stub.
        if not base.definition:
            base.definition = OFFLINE_DEFINITION
        return base
    return _offline_stub(cleaned)
