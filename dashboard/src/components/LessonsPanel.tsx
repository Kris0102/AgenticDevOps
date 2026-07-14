import { useEffect, useState } from 'react'
import { api, type LessonView } from '../api'
import { isHumanReviewed, origin, originLabel, whyText } from '../lessonInfo'

// What this run's repo knows (ADR 0018 + ADR 0034) — the agent-learned lessons plus the
// human's operator rules, exactly as the Lesson store page shows them but read-only and
// scoped to the open run. Provenance comes from the shared lessonInfo derivation, so the
// two views can never drift. Curation lives on the store page (top bar → Lessons).
export function LessonsPanel({ runId, live }: { runId: string; live: boolean }) {
  const [lessons, setLessons] = useState<LessonView[]>([])

  useEffect(() => {
    let alive = true
    const load = () => { api.getLessons(runId).then((ls) => { if (alive) setLessons(ls) }).catch(() => {}) }
    setLessons([])
    load()
    if (!live) return () => { alive = false }
    const t = setInterval(load, 3000)
    return () => { alive = false; clearInterval(t) }
  }, [runId, live])

  return (
    <div className="panel lessons">
      <h3>repo lessons <span className="rh-count">{lessons.length}</span></h3>
      {lessons.length === 0 && (
        <p className="empty-hint">no confirmed lessons for this repo yet — they're written when a reviewer approves a revised plan or a failed microtask finally passes</p>
      )}
      <div className="llist">
        {lessons.map((l) => (
          <div key={l.lesson_id} className={`lrow scope-${l.scope} kind-${origin(l)}`}>
            <span className="lscope">{l.scope}</span>
            <div className="lbody">
              <span className="lsummary">{l.summary}</span>
              {l.guidance && <span className="lguide">fix: {l.guidance}</span>}
              <span className="lwhy">{whyText(l)}</span>
            </div>
            <div className="lmeta">
              <span className="lorigin">{originLabel(l)}</span>
              {l.injected === false && (
                <span className="lsdropped" title="past the per-scope rule limit — stored but never sent to the agents">
                  not sent
                </span>
              )}
              {l.failure_type && <span className="ltype">{l.failure_type}</span>}
              {isHumanReviewed(l) && <span className="lsedited">edited</span>}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
