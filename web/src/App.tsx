import { useEffect, useMemo, useState } from 'react'
import './App.css'
import { applyGrade, dueQueue, formatIntervalHint } from './sm2'
import {
  fetchWords,
  loadCardStore,
  loadSettings,
  mergeWords,
  saveCardStore,
  saveSettings,
} from './storage'
import type { Grade, ReviewCard, Settings } from './types'

type Screen = 'review' | 'settings'

export default function App() {
  const [settings, setSettings] = useState<Settings>(() => loadSettings())
  const [store, setStore] = useState(() => loadCardStore())
  const [flipped, setFlipped] = useState(false)
  const [screen, setScreen] = useState<Screen>('review')
  const [syncing, setSyncing] = useState(false)
  const [status, setStatus] = useState<string | null>(null)
  const [tick, setTick] = useState(() => Date.now())

  // Refresh due queue periodically (learning steps are minute-scale)
  useEffect(() => {
    const id = window.setInterval(() => setTick(Date.now()), 15_000)
    return () => window.clearInterval(id)
  }, [])

  const cards = useMemo(() => Object.values(store.cards), [store.cards])
  const queue = useMemo(() => dueQueue(cards, tick), [cards, tick])
  const current: ReviewCard | undefined = queue[0]
  const remaining = queue.length

  function persist(next: typeof store) {
    setStore(next)
    saveCardStore(next)
  }

  function grade(g: Grade) {
    if (!current) return
    const updated = applyGrade(current, g)
    const next = {
      ...store,
      cards: { ...store.cards, [String(updated.id)]: updated },
    }
    persist(next)
    setFlipped(false)
    setTick(Date.now())
  }

  async function sync() {
    if (!settings.apiKey.trim()) {
      setStatus('Add an API key in Settings first.')
      setScreen('settings')
      return
    }
    setSyncing(true)
    setStatus(null)
    try {
      const words = await fetchWords(settings)
      const next = mergeWords(store, words)
      persist(next)
      setStatus(`Synced ${words.length} word${words.length === 1 ? '' : 's'}.`)
      setTick(Date.now())
      setScreen('review')
    } catch (err) {
      setStatus(err instanceof Error ? err.message : 'Sync failed')
    } finally {
      setSyncing(false)
    }
  }

  function saveSettingsForm(next: Settings) {
    const cleaned = {
      baseUrl: next.baseUrl.trim().replace(/\/$/, ''),
      apiKey: next.apiKey.trim(),
    }
    setSettings(cleaned)
    saveSettings(cleaned)
    setStatus('Settings saved.')
  }

  return (
    <div className="app">
      <div className="atmosphere" aria-hidden="true" />
      <header className="top">
        <p className="brand">Wortschatz</p>
        <nav className="nav">
          <button
            type="button"
            className={screen === 'review' ? 'nav-btn active' : 'nav-btn'}
            onClick={() => setScreen('review')}
          >
            Review
          </button>
          <button
            type="button"
            className={screen === 'settings' ? 'nav-btn active' : 'nav-btn'}
            onClick={() => setScreen('settings')}
          >
            Settings
          </button>
          <button
            type="button"
            className="nav-btn sync"
            onClick={() => void sync()}
            disabled={syncing}
          >
            {syncing ? 'Syncing…' : 'Sync'}
          </button>
        </nav>
      </header>

      {status && (
        <p className="status" role="status">
          {status}
        </p>
      )}

      {screen === 'settings' ? (
        <SettingsPanel
          settings={settings}
          onSave={saveSettingsForm}
          lastSyncedAt={store.lastSyncedAt}
          cardCount={cards.length}
        />
      ) : (
        <main className="review">
          {cards.length === 0 ? (
            <EmptyState
              title="No cards yet"
              body="Open Settings, set your API key, then Sync to pull words from the pipeline."
              actionLabel="Open Settings"
              onAction={() => setScreen('settings')}
            />
          ) : !current ? (
            <EmptyState
              title="Done for now"
              body="Nothing is due. Sync for new words, or come back when learning steps mature."
            />
          ) : (
            <>
              <p className="meta">
                {remaining} due
                {current.phase === 'learning' ? ' · learning' : ''}
              </p>
              <button
                type="button"
                className={`card ${flipped ? 'flipped' : ''}`}
                onClick={() => setFlipped((f) => !f)}
                aria-label={flipped ? 'Hide answer' : 'Show answer'}
              >
                <div className="card-face front">
                  <h1 className="word">{current.target_word}</h1>
                  {current.grammar_details && (
                    <p className="grammar">{current.grammar_details}</p>
                  )}
                  <p className="hint">Tap to flip</p>
                </div>
                <div className="card-face back">
                  <p className="definition">{current.definition}</p>
                  {current.example_sentence && (
                    <p className="example">{current.example_sentence}</p>
                  )}
                  <p className="hint subtle">Tap to flip back</p>
                </div>
              </button>

              {flipped && (
                <div className="grades" role="group" aria-label="Grade">
                  {(['again', 'hard', 'good', 'easy'] as Grade[]).map((g) => (
                    <button
                      key={g}
                      type="button"
                      className={`grade grade-${g}`}
                      onClick={() => grade(g)}
                    >
                      <span className="grade-label">
                        {g.charAt(0).toUpperCase() + g.slice(1)}
                      </span>
                      <span className="grade-hint">
                        {formatIntervalHint(current, g)}
                      </span>
                    </button>
                  ))}
                </div>
              )}
            </>
          )}
        </main>
      )}
    </div>
  )
}

function EmptyState({
  title,
  body,
  actionLabel,
  onAction,
}: {
  title: string
  body: string
  actionLabel?: string
  onAction?: () => void
}) {
  return (
    <div className="empty">
      <h1>{title}</h1>
      <p>{body}</p>
      {actionLabel && onAction && (
        <button type="button" className="primary" onClick={onAction}>
          {actionLabel}
        </button>
      )}
    </div>
  )
}

function SettingsPanel({
  settings,
  onSave,
  lastSyncedAt,
  cardCount,
}: {
  settings: Settings
  onSave: (s: Settings) => void
  lastSyncedAt: number | null
  cardCount: number
}) {
  const [baseUrl, setBaseUrl] = useState(settings.baseUrl)
  const [apiKey, setApiKey] = useState(settings.apiKey)

  useEffect(() => {
    setBaseUrl(settings.baseUrl)
    setApiKey(settings.apiKey)
  }, [settings])

  return (
    <main className="settings">
      <h1>Settings</h1>
      <p className="lede">
        Point at the live pipeline and sync the word bank into local SM-2
        scheduling (stored in this browser only).
      </p>
      <label className="field">
        <span>Base URL</span>
        <input
          type="url"
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          placeholder="https://wortschatz.eleanatanrui.com"
          autoComplete="off"
        />
      </label>
      <label className="field">
        <span>API key</span>
        <input
          type="password"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          placeholder="SECURE_API_KEY"
          autoComplete="off"
        />
      </label>
      <button
        type="button"
        className="primary"
        onClick={() => onSave({ baseUrl, apiKey })}
      >
        Save
      </button>
      <p className="footnote">
        {cardCount} card{cardCount === 1 ? '' : 's'} locally
        {lastSyncedAt
          ? ` · last sync ${new Date(lastSyncedAt).toLocaleString()}`
          : ' · never synced'}
      </p>
    </main>
  )
}
