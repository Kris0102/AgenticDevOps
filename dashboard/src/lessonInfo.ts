import type { LessonView } from './api'

// How a lesson got into the store, DERIVED from the lesson's own fields (ADR 0034) —
// no `origin` column, nothing describing the provenance stored twice. Both the run-page
// panel and the lesson store page render from here, so they always read identically.
//
// The derivation is only possible because lessons are written on CONFIRMATION, never at
// the moment of failure (ADR 0018): `agent` + `scope` pin down exactly which confirming
// step wrote the row.

export type LessonOrigin = 'human' | 'plan' | 'code' | 'unknown'

export function origin(l: LessonView): LessonOrigin {
  if (l.agent === 'human') return 'human'
  if (l.scope === 'plan') return 'plan'
  if (l.scope === 'code') return 'code'
  return 'unknown'
}

// A human edit is the ONLY thing that sets updated_at — which is also what makes the
// lesson immune to a failed run's retraction. Same fact, one source.
export function isHumanReviewed(l: LessonView): boolean {
  return Boolean(l.updated_at)
}

// The "why was this added" sentence. Present tense, no jargon the grader would not have.
// Note the two distinct roles it names: the agent that DIAGNOSED the failure never gets to
// bless its own diagnosis — a different agent's outcome is what promoted it to a lesson
// (ADR 0018). That separation is the whole reason these can be trusted, so it is stated.
export function whyText(l: LessonView): string {
  switch (origin(l)) {
    case 'human':
      return 'Written by you as a standing rule for this repo. Always sent to the agents — it is not ranked against the learned lessons and never expires.'
    case 'plan':
      return 'The plan reviewer rejected the plan for this reason. It was kept once the revised plan was approved — the rejection turned out to be right.'
    case 'code':
      return `The reflector diagnosed this after ${l.microtask_id ? `microtask ${l.microtask_id}` : 'a microtask'} failed its tests. It was kept once the tester confirmed that microtask passing — the fix demonstrably worked.`
    default:
      return 'Confirmed by a past run on this repo.'
  }
}

// Short badge for the card header — who WROTE the insight.
//
// Deliberately derived from `scope`, not from the raw `agent` column: `agent` records the
// node that CONFIRMED the lesson and wrote the row, which for a code lesson is the tester,
// not the reflector whose diagnosis the row actually contains. Rendering `agent` here would
// credit the tester for the reflector's work. Scope maps 1:1 onto the author because each
// scope has exactly one diagnosing agent (plan → reviewer, code → reflector).
export function originLabel(l: LessonView): string {
  switch (origin(l)) {
    case 'human': return 'operator rule'
    case 'plan': return 'learned by the plan reviewer'
    case 'code': return 'learned by the reflector'
    default: return 'learned by a past run'
  }
}

// Where a lesson goes at run time. Scope is not decoration — it decides the audience.
export function audience(scope: string): string {
  if (scope === 'plan') return 'sent to the planner + plan reviewer'
  if (scope === 'code') return 'sent to the coder + reflector'
  return 'sent to the agents'
}
