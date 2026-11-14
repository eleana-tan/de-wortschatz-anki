/** Row shape from GET /words */
export type ApiWord = {
  id: number
  raw_input?: string | null
  source?: string | null
  target_word: string
  grammar_details?: string | null
  definition: string
  example_sentence?: string | null
  exported: number
  created_at?: string
  updated_at?: string
}

export type Grade = 'again' | 'hard' | 'good' | 'easy'

export type CardPhase = 'learning' | 'review'

/** Local SM-2 scheduling state + content snapshot from API */
export type ReviewCard = {
  id: number
  target_word: string
  grammar_details: string
  definition: string
  example_sentence: string
  exported: number
  /** SM-2 */
  ease: number
  interval_days: number
  due_at: number
  reps: number
  lapses: number
  phase: CardPhase
  /** learning step index (0 = first step) */
  learning_step: number
}

export type Settings = {
  baseUrl: string
  apiKey: string
}

export type CardStore = {
  cards: Record<string, ReviewCard>
  lastSyncedAt: number | null
}
