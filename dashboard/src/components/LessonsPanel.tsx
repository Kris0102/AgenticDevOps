import { useEffect, useState } from 'react'
import { api, type LessonView } from '../api'

// What past runs on this repo taught the system (ADR 0018) — confirmed failure lessons
// the agents get injected as advisory context. Renders nothing when the repo has none.
export function LessonsPanel({ runId }: { runId: string }) {
  const [lessons, setLessons] = useState<LessonView[]>([])

  useEffect(() => {
    setLessons([])
    api.getLessons(runId).then(setLessons).catch(() => {})
  }, [runId])

  return (
    <div className="panel lessons">
      <h3>repo lessons <span className="rh-count">{lessons.length}</span></h3>
      {lessons.length === 0 && (
        <p className="empty-hint">no confirmed lessons for this repo yet — they're written when a reviewer approves a revised plan or a failed microtask finally passes</p>
      )}
      <div className="llist">
        {lessons.map((l) => (
          <div key={l.lesson_id} className={`lrow scope-${l.scope}`}>
            <span className="lscope">{l.scope}</span>
            <div className="lbody">
              <span className="lsummary">{l.summary}</span>
              {l.guidance && <span className="lguide">fix: {l.guidance}</span>}
            </div>
            {l.failure_type && <span className="ltype">{l.failure_type}</span>}
          </div>
        ))}
      </div>
    </div>
  )
}
