import type { Grade, ReviewCard } from './types'

/** Minimal Anki-ish SM-2 for browser prototype. */

const EASE_START = 2.5
const EASE_FLOOR = 1.3
const HARD_FACTOR = 1.2
const EASY_BONUS = 1.3
/** Learning steps in minutes; graduate after Good/Easy from last step. */
const LEARNING_STEPS_MIN = [1, 10]
const GRADUATING_IVL_DAYS = 1
const EASY_IVL_DAYS = 4

const MINUTE_MS = 60_000
const DAY_MS = 86_400_000

export function newCardDefaults(now = Date.now()): Pick<
  ReviewCard,
  'ease' | 'interval_days' | 'due_at' | 'reps' | 'lapses' | 'phase' | 'learning_step'
> {
  return {
    ease: EASE_START,
    interval_days: 0,
    due_at: now,
    reps: 0,
    lapses: 0,
    phase: 'learning',
    learning_step: 0,
  }
}

function clampEase(ease: number): number {
  return Math.max(EASE_FLOOR, Math.round(ease * 100) / 100)
}

function scheduleLearning(
  card: ReviewCard,
  grade: Grade,
  now: number,
): ReviewCard {
  const next = { ...card }

  if (grade === 'again') {
    next.learning_step = 0
    next.lapses += 1
    next.due_at = now + LEARNING_STEPS_MIN[0] * MINUTE_MS
    return next
  }

  if (grade === 'hard') {
    // Stay on current step; short delay
    const mins = LEARNING_STEPS_MIN[Math.min(next.learning_step, LEARNING_STEPS_MIN.length - 1)]
    next.due_at = now + Math.max(1, mins / 2) * MINUTE_MS
    return next
  }

  // Good / Easy — advance or graduate
  const lastStep = LEARNING_STEPS_MIN.length - 1
  if (grade === 'easy' || next.learning_step >= lastStep) {
    next.phase = 'review'
    next.learning_step = 0
    next.reps += 1
    next.interval_days = grade === 'easy' ? EASY_IVL_DAYS : GRADUATING_IVL_DAYS
    if (grade === 'easy') {
      next.ease = clampEase(next.ease + 0.15)
    }
    next.due_at = now + next.interval_days * DAY_MS
    return next
  }

  next.learning_step += 1
  next.reps += 1
  next.due_at = now + LEARNING_STEPS_MIN[next.learning_step] * MINUTE_MS
  return next
}

function scheduleReview(card: ReviewCard, grade: Grade, now: number): ReviewCard {
  const next = { ...card }
  const delayDays = Math.max(0, (now - card.due_at) / DAY_MS)
  const ivl = Math.max(1, card.interval_days || 1)

  if (grade === 'again') {
    next.lapses += 1
    next.ease = clampEase(next.ease - 0.2)
    next.phase = 'learning'
    next.learning_step = 0
    next.interval_days = 0
    next.due_at = now + LEARNING_STEPS_MIN[0] * MINUTE_MS
    return next
  }

  next.reps += 1

  if (grade === 'hard') {
    next.ease = clampEase(next.ease - 0.15)
    next.interval_days = Math.max(1, Math.round(ivl * HARD_FACTOR))
  } else if (grade === 'good') {
    next.interval_days = Math.max(
      1,
      Math.round((ivl + delayDays / 2) * next.ease),
    )
  } else {
    // easy
    next.ease = clampEase(next.ease + 0.15)
    next.interval_days = Math.max(
      1,
      Math.round((ivl + delayDays) * next.ease * EASY_BONUS),
    )
  }

  next.due_at = now + next.interval_days * DAY_MS
  return next
}

/** Apply grade and return updated card scheduling fields. */
export function applyGrade(card: ReviewCard, grade: Grade, now = Date.now()): ReviewCard {
  if (card.phase === 'learning') {
    return scheduleLearning(card, grade, now)
  }
  return scheduleReview(card, grade, now)
}

/** Due cards sorted by due_at ascending. */
export function dueQueue(cards: ReviewCard[], now = Date.now()): ReviewCard[] {
  return cards
    .filter((c) => c.due_at <= now)
    .sort((a, b) => a.due_at - b.due_at || a.id - b.id)
}

export function formatIntervalHint(card: ReviewCard, grade: Grade, now = Date.now()): string {
  const preview = applyGrade(card, grade, now)
  const delta = preview.due_at - now
  if (delta < DAY_MS) {
    const mins = Math.max(1, Math.round(delta / MINUTE_MS))
    return mins < 60 ? `${mins}m` : `${Math.round(mins / 60)}h`
  }
  const days = Math.round(delta / DAY_MS)
  return days === 1 ? '1d' : `${days}d`
}
