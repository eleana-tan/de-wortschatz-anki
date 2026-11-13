# German Wortschatz → Anki Pipeline

Headless Python **FastAPI** backend for DE→EN vocabulary capture (iOS Shortcuts), hybrid enrichment (local **dict.cc SQLite FTS5** + **Gemini 2.5 Flash**), and **genanki** `.apkg` export.

## Features

- `POST /capture` — Bearer-auth capture → enrich → `word_bank` (`exported = 0`)
- `GET /words` — Bearer-auth JSON list of `word_bank` rows (for review sync)
- `GET /export` — build Anki deck from unexported rows, mark `exported = 1`
- `GET /health` — liveness + DB / FTS counts
- `web/` — SM-2 flashcard review SPA (Vite + React; schedules in `localStorage`)
- `ingest_dictcc.py` — load dict.cc German–English TSV into FTS5

## Requirements

- Python 3.10+
- Free LLM key (optional but recommended): [Google AI Studio](https://aistudio.google.com/) for Gemini
- dict.cc German–English word list (download from dict.cc → About → Download word list; **do not commit**)

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env: SECURE_API_KEY; optionally GOOGLE_API_KEY
```

Generate a strong key:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### Ingest dict.cc

Download the German–English word list from dict.cc (About → Download word list). **Do not commit** the dump or resulting `*.db` files.

```bash
mkdir -p data
# Default: clear + rebuild (idempotent — safe to re-run the same dump)
python ingest_dictcc.py /path/to/de-en.txt --db ./data/wortschatz.db

# Optional flags
python ingest_dictcc.py /path/to/de-en.txt --encoding utf-8   # force encoding
python ingest_dictcc.py /path/to/de-en.txt --append           # keep old rows (can duplicate)
python ingest_dictcc.py /path/to/de-en.txt -v                 # debug logging
python ingest_dictcc.py --help
```

What the ingest does:

| Concern | Behavior |
| --- | --- |
| Comments | Lines starting with `#` (license / headers) are skipped |
| Encoding | Auto-detects `utf-8-sig` → `utf-8` → `latin-1` → `cp1252` (override with `--encoding`) |
| Gender | `{m}` / `{f}` / `{n}` → `gender` column; stripped from searchable `de_term` |
| Articles | Leading `der`/`die`/`das`/`ein`/`eine` stripped from `de_term` (enricher rebuilds article from gender) |
| POS tags | `{adj}`, `{verb}`, … stored in `pos`; bracket notes `[…]` stripped from terms |
| Idempotency | **Default clear+rebuild** so a full dump reload does not duplicate; `--append` only when intentionally merging |
| Index | Writes `dictcc_entries` + external-content FTS5 `dictcc_fts` (same schema `lookup_dictcc_fts` / enricher use; DE lookups use `de_only=True`) |

### Run API (local)

```bash
uvicorn main:app --host 127.0.0.1 --port 8000
# or: python main.py
```

### Flashcard review UI (`web/`)

Browser prototype: flip German → English cards, grade Again / Hard / Good / Easy, SM-2-like intervals in `localStorage`. Sync pulls `GET /words` from the pipeline API.

```bash
cd web
npm install
npm run dev
# open the printed localhost URL (usually http://127.0.0.1:5173)
```

In **Settings**:

| Field | Typical value |
| --- | --- |
| Base URL | `https://wortschatz.eleanatanrui.com` (or `http://127.0.0.1:8000` for a local API) |
| API key | same `SECURE_API_KEY` as the server `.env` |

Then **Save** → **Sync**. Due cards appear on **Review**; when the queue is empty you see “Done for now.” Scheduling state never leaves the browser — only word content is fetched from the API. Do not commit API keys.

CORS is enabled on the API so a local Vite origin can call the live host with Bearer auth.

---

## Deploy (VPS)

Practical path: venv + `.env` + dict.cc ingest → systemd (uvicorn on localhost) → nginx + certbot HTTPS → curl smoke → iOS Shortcuts.

Public host: **`wortschatz.eleanatanrui.com`** (nginx reverse proxy + Let’s Encrypt via certbot — not Caddy).

### 1. Install on the VPS

```bash
# example layout
sudo mkdir -p /opt/wortschatz-anki
sudo chown "$USER":"$USER" /opt/wortschatz-anki
cd /opt/wortschatz-anki
# copy/clone this pipeline tree here

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
# edit .env — set SECURE_API_KEY (required) + optional GOOGLE_API_KEY
mkdir -p data exports
python ingest_dictcc.py /path/to/de-en.txt --db ./data/wortschatz.db
```

Bind uvicorn to **127.0.0.1** only (systemd example below). Do not expose port 8000 on the public interface.

### 2. systemd (uvicorn)

Example unit: [`deploy/vocab-anki.service.example`](deploy/vocab-anki.service.example).

```bash
sudo cp deploy/vocab-anki.service.example /etc/systemd/system/vocab-anki.service
# edit User=, WorkingDirectory=, EnvironmentFile=, ExecStart= paths
sudo systemctl daemon-reload
sudo systemctl enable --now vocab-anki
sudo systemctl status vocab-anki
```

### 3. nginx reverse proxy + HTTPS (certbot)

Production terminates TLS with **nginx + certbot** and proxies to `127.0.0.1:8000`. DNS for **`wortschatz.eleanatanrui.com`** should point at the VPS.

```bash
# after nginx site is configured for wortschatz.eleanatanrui.com → 127.0.0.1:8000
sudo nginx -t && sudo systemctl reload nginx
# certbot (example): sudo certbot --nginx -d wortschatz.eleanatanrui.com
curl -sS https://wortschatz.eleanatanrui.com/health
```

Keep `/capture` and `/export` behind HTTPS + Bearer — never publish them without auth.

Optional alternate: [`deploy/caddy.Caddyfile.example`](deploy/caddy.Caddyfile.example) (not used in production).

### 4. curl smoke tests

Replace host and key as needed (`BASE=https://wortschatz.eleanatanrui.com` or `http://127.0.0.1:8000`).

```bash
export BASE=https://wortschatz.eleanatanrui.com
export SECURE_API_KEY='…'   # same value as in .env

# /health — open, no auth
curl -sS "$BASE/health"

# /capture — Bearer + JSON body
curl -sS -X POST "$BASE/capture" \
  -H "Authorization: Bearer $SECURE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input":"Veranstaltung","source":"ios_voice"}'

# /export — Bearer; downloads .apkg (404 if queue empty)
curl -sS -OJ -X GET "$BASE/export" \
  -H "Authorization: Bearer $SECURE_API_KEY"

# expect 401 without a valid key
curl -sS -o /dev/null -w "%{http_code}\n" -X POST "$BASE/capture" \
  -H "Content-Type: application/json" \
  -d '{"input":"Haus"}'
```

### 5. iOS Shortcuts (brief)

Two Shortcuts typically: **Capture** (voice / share sheet) and **Export** (download `.apkg`).

| Piece | Value |
| --- | --- |
| URL | `https://wortschatz.eleanatanrui.com/capture` or `…/export` |
| Method | `POST` (`/capture`) · `GET` (`/export`) |
| Header | `Authorization` = `Bearer <SECURE_API_KEY>` |
| Header | `Content-Type` = `application/json` (capture only) |
| Body (capture) | JSON: `{"input":"<dictated or shared text>","source":"ios_voice"}` or `"ios_share"` |

Store the key in a Shortcut password field / Ask Each Time — do not paste it into shared Shortcut links. For export, save the response body as a file (`.apkg`) and open/share into AnkiMobile if installed.

### 6. Security reminders

- **`SECURE_API_KEY` required** — non-empty; process refuses to start if missing/blank. Generate with `secrets.token_urlsafe(32)`.
- **No public `/capture`** — always HTTPS via nginx + certbot; uvicorn on `127.0.0.1` only; Bearer on `/capture`, `/words`, and `/export`. `/health` is intentionally open (no secrets).
- Never commit `.env`, `*.db`, dict.cc dumps, or `.apkg` exports (see `.gitignore`).
- Rotate `SECURE_API_KEY` if a Shortcut or device is compromised.
- Prefer a dedicated Linux user for the service (`User=` in the unit file).

---

## API

### Auth

```http
Authorization: Bearer <SECURE_API_KEY>
```

Required for `/capture`, `/words`, and `/export` (`/health` is open). Comparison uses `secrets.compare_digest` (constant-time). Missing or wrong credentials return **401**. An empty or unset `SECURE_API_KEY` causes a **loud startup failure** — the process will not serve.

```bash
# 401 — no Authorization header
curl -sS -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8000/capture \
  -H "Content-Type: application/json" \
  -d '{"input":"Haus"}'

# 401 — wrong key
curl -sS -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8000/capture \
  -H "Authorization: Bearer wrong-key" \
  -H "Content-Type: application/json" \
  -d '{"input":"Haus"}'

# 401 — export without key
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/export
```

### Capture

```bash
curl -sS -X POST http://127.0.0.1:8000/capture \
  -H "Authorization: Bearer $SECURE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input":"Veranstaltung","source":"ios_voice"}'
```

### Words (review sync)

Returns every `word_bank` row as a JSON array (id, target_word, grammar_details, definition, example_sentence, exported, timestamps, etc.). Used by `web/` Sync.

```bash
curl -sS http://127.0.0.1:8000/words \
  -H "Authorization: Bearer $SECURE_API_KEY"
```

### Enrichment (hybrid)

1. **Local dict.cc (FTS5)** — exact German headword first, then `de_term` FTS; gender tags `{m}`/`{f}`/`{n}` → `der`/`die`/`das` on `target_word`.
2. **LLM** (`google-genai` / `gemini-3.8-flash`) — phonetic cleanup when `source` is `ios_voice` or there is no exact match; structured JSON fills plural in `grammar_details` plus one German `example_sentence`.
3. **No API key** — capture still succeeds: dict.cc gloss + gender only; `example_sentence` empty and plural omitted. If dict.cc also misses, definition is `(no dict.cc match; LLM unavailable)`.

Env: `GOOGLE_API_KEY`, `GEMINI_MODEL` (default `gemini-3.8-flash`).

Enrichment fields:

| Field | Example |
| --- | --- |
| `target_word` | `die Veranstaltung` |
| `grammar_details` | `Feminine • Pl: Veranstaltungen` |
| `definition` | `event; function` |
| `example_sentence` | `Die Veranstaltung findet morgen statt.` |

### Export

Builds a genanki `.apkg` from `word_bank` where `exported = 0`, then marks those rows `exported = 1` only after a successful write.

**HTTP** (downloadable attachment; requires Bearer auth):

```bash
curl -sS -OJ -X GET http://127.0.0.1:8000/export \
  -H "Authorization: Bearer $SECURE_API_KEY"
```

| Outcome | Status | Notes |
| --- | --- | --- |
| Pending rows | **200** | `Content-Disposition: attachment` `.apkg`; headers `X-Export-Count`, `X-Deck-Name` |
| Empty queue | **404** | `{"detail":"No unexported words in word_bank"}` — capture some words first |
| Auth missing/wrong | **401** | Same Bearer rules as `/capture` |

**CLI / save-to-disk** (same builder; writes under `EXPORT_DIR` by default):

```bash
python exporter.py                          # ./exports/german-wortschatz-*.apkg
python exporter.py -o ./my-deck.apkg        # explicit file
python exporter.py --db ./data/wortschatz.db --deck "German Wortschatz" --no-mark
```

Empty CLI queue exits with code **2** and prints `empty: …` on stderr.

Anki card layout:

- **Front:** `{{target_word}}` + `<small>{{grammar_details}}</small>`
- **Back:** `<b>{{definition}}</b>` + `<br><em>{{example_sentence}}</em>` (plus `{{FrontSide}}` above the answer divider)

### Health

```bash
curl -sS http://127.0.0.1:8000/health
```

## Project layout

| File | Role |
| --- | --- |
| `main.py` | FastAPI app |
| `database.py` | SQLite init + `word_bank` / `dictcc_fts` helpers |
| `ingest_dictcc.py` | TSV → FTS5 CLI |
| `enricher.py` | Hybrid FTS5 + LLM structured JSON |
| `exporter.py` | genanki deck builder |
| `web/` | Vite + React SM-2 review prototype |
| `.env.example` | Config template (no secrets) |
| `deploy/vocab-anki.service.example` | systemd unit for uvicorn |
| `deploy/caddy.Caddyfile.example` | Optional Caddy example (production uses nginx + certbot) |

## License

Use of dict.cc data is subject to dict.cc’s own terms; this repo does not redistribute their word list.
