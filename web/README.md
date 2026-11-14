# Wortschatz Review (`web/`)

SM-2 flashcard prototype for the German Wortschatz pipeline.

## Run

```bash
npm install
npm run dev
```

Open the Vite URL (default `http://127.0.0.1:5173`). In **Settings**, set:

- **Base URL:** `https://wortschatz.eleanatanrui.com`
- **API key:** your `SECURE_API_KEY` (never commit it)

**Save**, then **Sync** to pull `GET /words`. Review due cards with Again / Hard / Good / Easy.

## Build

```bash
npm run build
npm run preview
```

Scheduling state lives in `localStorage` (`wortschatz.cards`, `wortschatz.settings`).
