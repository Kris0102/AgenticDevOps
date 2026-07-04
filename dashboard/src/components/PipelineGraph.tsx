import { useEffect, useMemo, useState } from 'react'
import { api, type EventView, type RunResponse } from '../api'

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

export function PipelineGraph({ runId, run, live }: { runId: string; run: RunResponse | null; live: boolean }) {
  // The node trail, derived from the event ACTOR stream (deduped consecutive).
  const [trail, setTrail] = useState<NodeId[]>([])

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

  return (
    <div className="panel pipeline">
      <div className="pl-head">
        <h3>pipeline</h3>
        <span className={`pl-state ${failed ? 'bad' : done ? 'ok' : ''}`}>
          {done ? '✓ completed' : failed ? '✗ failed' : activeNode ? `● ${NODES.find((n) => n.id === activeNode)?.label ?? ''}` : ''}
        </span>
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
