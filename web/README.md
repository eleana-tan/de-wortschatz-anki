# Wortschatz Review (`web/`)

SM-2 flashcard SPA for the German Wortschatz pipeline. Built for **site-root** hosting (`base: '/'`) next to the FastAPI API.

## Run (local)

```bash
npm ci
npm run dev
```

Open the Vite URL (default `http://127.0.0.1:5173`). In **Settings**, set:

- **Base URL:** `https://wortschatz.eleanatanrui.com` (dev default) or `http://127.0.0.1:8000`
- **API key:** your `SECURE_API_KEY` (never commit it)

**Save**, then **Sync** to pull `GET /words`. Review due cards with Again / Hard / Good / Easy.

## Build (production)

```bash
npm ci
npm run build
```

Output: `dist/` (serve with nginx; see [`../deploy/nginx.wortschatz.conf.example`](../deploy/nginx.wortschatz.conf.example) and the root README **Deploy** section).

Production builds default **Base URL** to empty string (same-origin → `GET /words` on the current host). Settings remain editable. Preview locally:

```bash
npm run preview
```

Scheduling state lives in `localStorage` (`wortschatz.cards`, `wortschatz.settings`).
