import { useEffect, useMemo, useState } from 'react'
import { api, type EventView, type MicrotaskRow, type RunResponse } from '../api'

// Live pipeline map: the real LangGraph topology with the CURRENT node pulsing and the
// path the run has taken lit up. Derived from durable data only — `run.status` (polled)
// gives the active node, `run.status_changed` events give the traversed trail — so it
// renders the full journey for old runs too. Hand-rolled SVG: 9 fixed nodes don't need
// a graph library.

type NodeId =
  | 'bootstrap' | 'planner' | 'reviewer' | 'coder' | 'tester'
  | 'reflector' | 'deploy' | 'push' | 'hitl'

interface GNode {
  id: NodeId
  label: string
  icon: string
  x: number   // center
  y: number
  cat: string // --cat-* palette key
}

const W = 116
const H = 44

const NODES: GNode[] = [
  { id: 'bootstrap', label: 'bootstrap', icon: '⚙',   x: 85,  y: 170, cat: 'bootstrap' },
  { id: 'planner',   label: 'planner',   icon: '✎',   x: 225, y: 170, cat: 'planner' },
  { id: 'reviewer',  label: 'reviewer',  icon: '⌕',   x: 370, y: 170, cat: 'reviewer' },
  { id: 'coder',     label: 'coder',     icon: '‹/›', x: 520, y: 170, cat: 'coder' },
  { id: 'tester',    label: 'tester',    icon: '⚗',   x: 668, y: 170, cat: 'tester' },
  { id: 'deploy',    label: 'deploy',    icon: '⇡',   x: 810, y: 170, cat: 'deployer' },
  { id: 'push',      label: 'push',      icon: '⇈',   x: 952, y: 170, cat: 'deployer' },
  { id: 'reflector', label: 'reflector', icon: '⚕',   x: 594, y: 262, cat: 'reflector' },
  { id: 'hitl',      label: 'human gate', icon: '⏸',  x: 540, y: 62,  cat: 'hitl' },
]

interface GEdge {
  from: NodeId
  to: NodeId
  d: string        // svg path
  gate?: boolean   // dashed hitl edge
}

const EDGES: GEdge[] = [
  { from: 'bootstrap', to: 'planner',  d: 'M143,170 L167,170' },
  { from: 'planner',   to: 'reviewer', d: 'M283,170 L312,170' },
  { from: 'reviewer',  to: 'coder',    d: 'M428,170 L462,170' },
  { from: 'coder',     to: 'tester',   d: 'M578,170 L610,170' },
  { from: 'tester',    to: 'deploy',   d: 'M726,170 L752,170' },
  { from: 'deploy',    to: 'push',     d: 'M868,170 L890,170' },
  // loop-backs
  { from: 'reviewer',  to: 'planner',  d: 'M355,148 C325,116 270,116 240,148' },
  { from: 'tester',    to: 'coder',    d: 'M650,148 C615,118 573,118 538,148' },
  { from: 'tester',    to: 'reflector', d: 'M680,192 C688,222 660,240 636,251' },
  { from: 'reflector', to: 'coder',    d: 'M552,262 C516,258 508,222 514,194' },
  { from: 'reflector', to: 'tester',   d: 'M640,244 C660,230 668,214 668,194' },
  // hitl gate lanes (dashed)
  { from: 'reviewer',  to: 'hitl',     d: 'M390,148 C420,105 460,78 482,66', gate: true },
  { from: 'tester',    to: 'hitl',     d: 'M650,148 C625,105 620,80 598,66', gate: true },
  { from: 'deploy',    to: 'hitl',     d: 'M800,148 C760,84 660,62 598,60', gate: true },
]

// Blackboard status → the node that runs next (statuses route INTO nodes).
const STATUS_NODE: Record<string, NodeId> = {
  NEEDS_BOOTSTRAP: 'bootstrap',
  NEEDS_PLAN: 'planner',
  NEEDS_REVIEW: 'reviewer',
  NEEDS_CODE: 'coder',
  NEEDS_TEST: 'tester',
  REFLECT: 'reflector',
  NEEDS_HITL_APPROVAL: 'hitl',
  READY_TO_DEPLOY: 'push',
  DONE: 'deploy',
}

// Event actor → node. The actor stream is the reliable trail: every event an agent emits
// proves its node ran, whereas status transitions skip nodes (verified on real history).
const ACTOR_NODE: Record<string, NodeId> = {
  bootstrapper: 'bootstrap',
  planner: 'planner',
  reviewer: 'reviewer',
  coder: 'coder',
  tester: 'tester',
  reflector: 'reflector',
  deployer: 'deploy',
  human: 'hitl',
}

function nodeOf(e: EventView): NodeId | null {
  if (e.event_type.startsWith('hitl.')) return 'hitl'
  if (e.event_type === 'gitops.pushed') return 'push'
  return ACTOR_NODE[e.actor] ?? null   // orchestrator/db actors = routing noise, skipped
}

// Capacity meter: `max` segments, `value` of them lit. Reads as "how much headroom is
// left before the cap", not a fraction/target — the ambiguity a plain "1 / 6" has (looks
// like 6 is the goal, when it's actually the hard limit the run fails at). Falls back to
// text for caps too large to render as dots (a user-configured Constraints value).
function Meter({ value, max }: { value: number; max: number }) {
  if (max <= 0 || max > 12) return <span className="pp-cap">max {max}</span>
  const filled = Math.min(Math.max(value, 0), max)
  return (
    <>
      <span className="pp-meter" aria-hidden>
        {Array.from({ length: max }, (_, i) => (
          <span key={i} className={`pp-seg ${i < filled ? 'on' : ''}`} />
        ))}
      </span>
      <span className="pp-cap">max {max}</span>
    </>
  )
}

const MICROTASK_DONE = new Set(['COMPLETED', 'DONE'])

// Nodes that only ever run after the plan was approved — reaching any of them live
// (via activeNode) proves the coding cycle has begun, even before its own events commit.
const POST_PLAN_NODES = new Set<NodeId>(['coder', 'tester', 'reflector', 'deploy', 'push'])

export function PipelineGraph({ runId, run, live }: { runId: string; run: RunResponse | null; live: boolean }) {
  // The node trail, derived from the event ACTOR stream (deduped consecutive).
  const [trail, setTrail] = useState<NodeId[]>([])
  // Durable microtask board (not the checkpoint) — the fallback source once no microtask
  // is in-flight (`progress.microtask` is only ever the CURRENT one), so a finished run
  // still shows "N/M microtasks completed" instead of a stale "in progress" placeholder.
  const [tasks, setTasks] = useState<MicrotaskRow[]>([])

  useEffect(() => {
    let alive = true
    const load = () => api.getMicrotasks(runId).then((mts) => { if (alive) setTasks(mts) }).catch(() => {})
    setTasks([])
    load()
    if (!live) return () => { alive = false }
    const t = setInterval(load, 3000)
    return () => { alive = false; clearInterval(t) }
  }, [runId, live])

  useEffect(() => {
    let alive = true
    let cursor = 0
    let seq: NodeId[] = []
    const load = async () => {
      try {
        for (;;) {
          const batch = await api.getEvents(runId, cursor)
          if (!alive || batch.length === 0) break
          cursor = batch[batch.length - 1].seq
          for (const e of batch) {
            const node = nodeOf(e)
            if (node && node !== seq[seq.length - 1]) seq = [...seq, node]
          }
          if (batch.length < 200) break
        }
        if (alive) setTrail(seq)
      } catch { /* transient */ }
    }
    setTrail([]); seq = []
    load()
    if (!live) return () => { alive = false }
    const t = setInterval(load, 2500)
    return () => { alive = false; clearInterval(t) }
  }, [runId, live])

  const failed = run?.run_state === 'failed' || run?.status === 'FAILED'
  const done = run?.run_state === 'completed'

  const { visited, transitions, activeNode } = useMemo(() => {
    const trans = new Set<string>()
    for (let i = 0; i + 1 < trail.length; i++) trans.add(`${trail[i]}→${trail[i + 1]}`)
    const last = trail[trail.length - 1] ?? null
    // Live: the status names the node that runs NEXT (the actor stream lags one node);
    // failed: the last node that acted is where it died.
    const current = failed ? last : STATUS_NODE[String(run?.status ?? '')] ?? last
    return { visited: new Set<NodeId>(trail), transitions: trans, activeNode: done ? null : current }
  }, [trail, run?.status, failed, done])

  const nodeState = (id: NodeId): string => {
    if (activeNode === id) return failed ? 'failed' : 'active'
    if (visited.has(id)) return 'visited'
    return 'pending'
  }

  const edgeState = (e: GEdge): string => {
    if (transitions.has(`${e.from}→${e.to}`)) {
      return activeNode === e.to ? 'live' : 'traversed'
    }
    return 'idle'
  }

  const progress = run?.progress ?? null
  const planIter = progress?.planner_reviewer_iteration ?? 0
  const planMax = progress?.planner_reviewer_max ?? null
  const mt = progress?.microtask ?? null
  const mtTotal = tasks.length
  const mtDone = tasks.filter((t) => MICROTASK_DONE.has(t.status)).length
  // The plan cycle is done once we've moved past the reviewer — proof the plan was
  // approved and the run moved on (a reject just loops planner→reviewer again). Checked
  // two ways: `activeNode` reacts the instant `run.status` flips to NEEDS_CODE (committed
  // in the reviewer's own short transaction), so it flags this the moment coding STARTS;
  // `visited.has('coder')` is the fallback for when activeNode is null (run completed) or
  // parked at the hitl node (paused for the code/deploy gate) — by then the coder's own
  // events have long since committed (its node already finished), so the trail has caught up.
  const planDone = (activeNode !== null && POST_PLAN_NODES.has(activeNode)) || visited.has('coder')
  // What the plan cycle is doing RIGHT NOW, shown permanently alongside the pass count
  // (not only while iteration > 0) — plus a small completion note once it's done.
  const planStatus =
    activeNode === 'planner' ? 'planner drafting'
    : activeNode === 'reviewer' ? 'reviewer reviewing'
    : (run?.run_state === 'paused_hitl' && run.hitl_phase === 'plan') ? 'awaiting human review'
    : planDone ? 'plan approved ✓'
    : null

  return (
    <div className="panel pipeline">
      <div className="pl-head">
        <h3>pipeline</h3>
        <span className={`pl-state ${failed ? 'bad' : done ? 'ok' : ''}`}>
          {done ? '✓ completed' : failed ? '✗ failed' : activeNode ? `● ${NODES.find((n) => n.id === activeNode)?.label ?? ''}` : ''}
        </span>
      </div>

      <div className="pl-progress">
        <div className="pp-item pp-plan" title="planner ⇄ reviewer passes — the cap is the hard limit the run fails at, not a target">
          <span className="pp-ico" aria-hidden>▤</span>
          <div className="pp-body">
            {progress ? (
              <>
                <span className="pp-label">planner cycle: iteration {planIter}{planStatus ? ` · ${planStatus}` : ''}</span>
                <span className="pp-meterrow">
                  <Meter value={planIter} max={planMax ?? 0} />
                </span>
              </>
            ) : (
              <span className="pp-label pp-dim">plan cycle not started yet</span>
            )}
          </div>
        </div>

        <div className={`pp-item pp-microtask ${mt || mtTotal > 0 ? '' : 'pp-empty'}`} title="coder ⇄ tester ⇄ reflector loop">
          <span className="pp-ico" aria-hidden>◧</span>
          <div className="pp-body">
            {mt ? (
              <>
                <span className="pp-label">microtask #{mt.idx} of {mt.total} — attempt {mt.attempts}</span>
                <span className="pp-meterrow">
                  <Meter value={mt.attempts} max={mt.max_attempts} />
                </span>
              </>
            ) : mtTotal > 0 ? (
              <>
                <span className="pp-label">{mtDone}/{mtTotal} microtasks completed</span>
                <span className="pp-subline">
                  {tasks.map((t) => `#${t.idx} ${t.attempts} attempt${t.attempts === 1 ? '' : 's'}`).join('  ·  ')}
                </span>
              </>
            ) : (
              <span className="pp-label pp-dim">no microtasks have been created yet</span>
            )}
          </div>
        </div>
      </div>

      <svg viewBox="0 0 1020 300" className="plsvg" role="img" aria-label="run pipeline graph">
        <defs>
          <marker id="arr" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,0 L8,4 L0,8 z" fill="currentColor" />
          </marker>
        </defs>

        {EDGES.map((e) => (
          <path
            key={`${e.from}-${e.to}`}
            d={e.d}
            className={`pledge ${e.gate ? 'gate' : ''} ${edgeState(e)}`}
            markerEnd="url(#arr)"
          />
        ))}

        {NODES.map((n) => {
          const st = nodeState(n.id)
          return (
            <g key={n.id} className={`plnode ${st}`} style={{ ['--node-c' as string]: `var(--cat-${n.cat})` }}>
              {st === 'active' && (
                <rect x={n.x - W / 2} y={n.y - H / 2} width={W} height={H} rx={12} className="pulse" />
              )}
              <rect x={n.x - W / 2} y={n.y - H / 2} width={W} height={H} rx={12} className="box" />
              <text x={n.x - W / 2 + 14} y={n.y + 5} className="picon">{n.icon}</text>
              <text x={n.x + 10} y={n.y + 5} className="plabel" textAnchor="middle">{n.label}</text>
            </g>
          )
        })}
      </svg>
    </div>
  )
}
