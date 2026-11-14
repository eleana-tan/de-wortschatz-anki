import type { ApiWord, CardStore, ReviewCard, Settings } from './types'
import { newCardDefaults } from './sm2'

const SETTINGS_KEY = 'wortschatz.settings'
const CARDS_KEY = 'wortschatz.cards'

const DEFAULT_SETTINGS: Settings = {
  baseUrl: 'https://wortschatz.eleanatanrui.com',
  apiKey: '',
}

export function loadSettings(): Settings {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY)
    if (!raw) return { ...DEFAULT_SETTINGS }
    const parsed = JSON.parse(raw) as Partial<Settings>
    return {
      baseUrl: (parsed.baseUrl || DEFAULT_SETTINGS.baseUrl).replace(/\/$/, ''),
      apiKey: parsed.apiKey ?? '',
    }
  } catch {
    return { ...DEFAULT_SETTINGS }
  }
}

export function saveSettings(settings: Settings): void {
  localStorage.setItem(
    SETTINGS_KEY,
    JSON.stringify({
      baseUrl: settings.baseUrl.replace(/\/$/, ''),
      apiKey: settings.apiKey,
    }),
  )
}

export function loadCardStore(): CardStore {
  try {
    const raw = localStorage.getItem(CARDS_KEY)
    if (!raw) return { cards: {}, lastSyncedAt: null }
    const parsed = JSON.parse(raw) as CardStore
    return {
      cards: parsed.cards ?? {},
      lastSyncedAt: parsed.lastSyncedAt ?? null,
    }
  } catch {
    return { cards: {}, lastSyncedAt: null }
  }
}

export function saveCardStore(store: CardStore): void {
  localStorage.setItem(CARDS_KEY, JSON.stringify(store))
}

/** Merge API words into local store; keep existing SM-2 state for known ids. */
export function mergeWords(store: CardStore, words: ApiWord[], now = Date.now()): CardStore {
  const cards: Record<string, ReviewCard> = { ...store.cards }

  for (const w of words) {
    const key = String(w.id)
    const existing = cards[key]
    const content = {
      id: w.id,
      target_word: w.target_word,
      grammar_details: w.grammar_details ?? '',
      definition: w.definition,
      example_sentence: w.example_sentence ?? '',
      exported: w.exported,
    }
    if (existing) {
      cards[key] = { ...existing, ...content }
    } else {
      cards[key] = { ...content, ...newCardDefaults(now) }
    }
  }

  return { cards, lastSyncedAt: now }
}

export async function fetchWords(settings: Settings): Promise<ApiWord[]> {
  const base = settings.baseUrl.replace(/\/$/, '')
  const res = await fetch(`${base}/words`, {
    headers: {
      Authorization: `Bearer ${settings.apiKey}`,
      Accept: 'application/json',
    },
  })
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(
      res.status === 401
        ? 'Unauthorized — check API key'
        : `Sync failed (${res.status})${detail ? `: ${detail.slice(0, 120)}` : ''}`,
    )
  }
  return (await res.json()) as ApiWord[]
}
