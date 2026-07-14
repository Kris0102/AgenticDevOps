import { useEffect, useState } from 'react'
import { api, type HitlReviewContext } from '../api'
import { PlanReview } from './PlanReview'
import { DiffReview } from './DiffReview'

// The gate. Fetches review context (plan or diff), then approve / reject(+note).
// On submit, the status poll picks up the new run_state and this panel unmounts.
export function HitlPanel({ runId, phase }: { runId: string; phase: string }) {
  const [ctx, setCtx] = useState<HitlReviewContext | null>(null)
  const [note, setNote] = useState('')
  const [commitMsg, setCommitMsg] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let dead = false
    api
      .getHitl(runId)
      .then((c) => {
        if (dead) return
        setCtx(c)
        setCommitMsg(c?.deploy_commit_message ?? '')
      })
      .catch((e) => !dead && setErr(String(e)))
    return () => {
      dead = true
    }
  }, [runId, phase])

  const decide = async (decision: 'approve' | 'reject') => {
    if (decision === 'reject' && !note.trim()) {
      setErr('Reject requires a note.')
      return
    }
    setBusy(true)
    setErr(null)
    try {
      // Commit message only rides a deploy-gate approve — the push uses it verbatim.
      const commitOverride =
        decision === 'approve' && phase === 'deploy' ? commitMsg.trim() || undefined : undefined
      await api.resume(runId, decision, note.trim() || undefined, commitOverride)
      setNote('')
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="panel hitl">
      <h3>⏸ Human review — {phase} gate</h3>
      {ctx?.prompt && <p className="prompt">{ctx.prompt}</p>}

      {!ctx && !err && <div className="loading">loading review context…</div>}
      {ctx?.plan && <PlanReview plan={ctx.plan} />}
      {ctx?.diff && <DiffReview diff={ctx.diff} />}

      {err && <div className="error">{err}</div>}

      <div className="decide">
        {phase === 'deploy' && (
          <label className="commit-message">
            Commit message for the push
            {/* Prefilled with the real default the push would use (subject + objective +
                microtasks + files) — a multi-line git message, so a textarea, not an input.
                Edit to override; approving as-is pushes exactly what is shown. */}
            <textarea
              rows={10}
              value={commitMsg}
              onChange={(e) => setCommitMsg(e.target.value)}
              placeholder={`AgenticDevOps run ${runId}`}
            />
          </label>
        )}
        <textarea
          placeholder="note (required to reject — fed back to planner/reflector)"
          value={note}
          onChange={(e) => setNote(e.target.value)}
        />
        <div className="row">
          <button className="approve" disabled={busy} onClick={() => decide('approve')}>
            ✓ Approve
          </button>
          <button className="reject" disabled={busy} onClick={() => decide('reject')}>
            ✗ Reject
          </button>
        </div>
      </div>
    </section>
  )
}
