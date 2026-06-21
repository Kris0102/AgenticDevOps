import type { PlanView } from '../api'

// What the human sees at a `plan` gate: the plan summary + the microtasks it produced.
export function PlanReview({ plan }: { plan: PlanView }) {
  return (
    <div className="plan">
      <p className="summary">{plan.summary}</p>
      {plan.architecture_notes && <p className="notes">{plan.architecture_notes}</p>}

      {plan.risks.length > 0 && (
        <div className="riskbox">
          <b>Risks</b>
          <ul>
            {plan.risks.map((r, i) => (
              <li key={i}>{r}</li>
            ))}
          </ul>
        </div>
      )}

      <h4>Microtasks ({plan.microtasks.length})</h4>
      <ol className="microtasks">
        {plan.microtasks.map((m) => (
          <li key={m.microtask_id}>
            <div className="mtitle">
              <b>{m.title}</b>
              <span className="mstatus">{m.status}</span>
            </div>
            <div className="mdesc">{m.description}</div>
            {m.target_files.length > 0 && (
              <div className="mfiles">{m.target_files.join(', ')}</div>
            )}
          </li>
        ))}
      </ol>
    </div>
  )
}
