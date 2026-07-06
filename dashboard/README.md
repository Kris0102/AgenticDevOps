# AgenticDevOps Dashboard

Web frontend for the multi-agent control plane (`src/api/`, ADR 0013). Single-user,
localhost. It triggers runs, registers repositories, streams live logs, drives the
human-in-the-loop (HITL) approval gates, and browses the durable record of past runs.

It is **not** a second engine — it is a thin client over the FastAPI control API. Every
piece of state it shows comes from an HTTP endpoint; the dashboard holds no run state of
its own. If a behaviour looks wrong, the question is almost always "what did the API
return", not "what does the component remember".

---

## Run it (development)

```bash
# 1. Start the control API from the repo root (needs Postgres up — see ../CLAUDE.md)
cd ../src && ../venv/bin/uvicorn api.app:app --port 8000

# 2. Start the dashboard (this directory)
npm install
npm run dev            # http://localhost:5173
```

The Vite dev server proxies API paths to uvicorn on `:8000` so the browser sees **one
origin** (no CORS). The proxied path prefixes are listed in `vite.config.ts`:

```
/runs      → :8000     (runs + SSE log stream)
/settings  → :8000     (settings hub)
/repos     → :8000     (repo registry)
```

> **Adding a new top-level API path?** You must add its prefix to `vite.config.ts`,
> otherwise the dev server answers with the SPA's own `index.html` (HTTP 200) and the
> calling code receives HTML where it expected JSON. This is the single most common
> "the page went blank" cause in dev.

Other scripts: `npm run build` (typecheck + production bundle into `dist/`),
`npm run preview` (serve the built bundle).

---

## Stack

React 18 + TypeScript + Vite. No router (view state is a `useState` union in `App.tsx`),
no state library (local state + polling is enough for one user). `axios` for HTTP,
native `EventSource` for the SSE log stream, `react-diff-view` for the green/red diff.

---

## Mental model: how the UI learns what happened

The backend drives a **checkpointed LangGraph** that suspends at HITL gates. The
dashboard never pushes the graph forward on its own — it observes and, at a gate, relays
the human's verdict. Two mechanisms carry all of that:

| Mechanism | Used for | Where |
|---|---|---|
| **Polling** `GET /runs/{id}` every ~1.5s | the run's `run_state` + which HITL gate (if any) is waiting | `hooks/useRunStatus.ts` |
| **SSE** `GET /runs/{id}/logs/stream` | live, granular log lines (ephemeral) | `hooks/useLogStream.ts` |

Plus two **poll-on-demand** feeds for durable history that survives a restart:
`GET /runs/{id}/events?after=<seq>` (the audit timeline) and the artifacts / microtasks /
lessons endpoints.

Live vs durable is a deliberate split (ADR 0013): the log stream is a live tail of an
in-memory ring buffer — empty once a run finishes — while the events table is the
permanent record. That is why a **finished** run shows the event timeline full-width and
hides the (now-empty) log panel.

Two SSE gotchas already handled in `useLogStream`, worth knowing before you touch it:
- The server **replays its whole ring buffer** to every new connection, and
  `EventSource` auto-reconnects (and React StrictMode double-mounts in dev). Frames are
  therefore **deduped by `seq`** — without it, one error line appears N times and looks
  like a real loop.
- The caller passes `active`; once the run is terminal it flips to `false` so the
  `EventSource` closes instead of reconnecting onto a drained stream.

---

## The two screens

`App.tsx` is the whole router. `view` (`'home' | 'newrun' | 'settings' | 'repos'`)
picks a full-page route when no run is open; a set `runId` opens the run-detail screen.

### 1. Home / run detail

- **`RunsHome`** — grid of every past run (`GET /runs`), filterable/sortable, plus a
  pulse strip of aggregate counts. Click a box → opens that run.
- **Run detail** (a `runId` is set) stacks:
  - `StatusBar` — lifecycle badge + the newest live-log line inline ("now doing").
  - `PipelineGraph` — the agent graph as an SVG, lighting up the active node/edge.
  - a **failure banner** / **stalled continue-bar** / **`HitlPanel`**, whichever the
    `run_state` calls for.
  - `RunOverview` — task, phase gantt, microtask board.
  - a tab strip: **activity** (`LogPanel` live + `EventTimeline` durable),
    **artifacts** (`ArtifactsPanel`), **lessons** (`LessonsPanel`).

The **HITL gate** is the one place the human acts on the graph. `HitlPanel` fetches
`GET /runs/{id}/hitl` — which returns a **plan + microtasks** for a `plan` gate (rendered
by `PlanReview`) or a **raw green/red diff** for a `code` gate (`DiffReview`) — then
`POST /runs/{id}/resume` with approve or reject(+note). A reject **requires** a note; it
is fed back to the planner/reflector. After submit, the status poll picks up the new
`run_state` and the panel unmounts.

### 2. Settings / Repos (full-page, no run open)

- **`SettingsPage`** — the settings hub (ADR 0020): LLM providers (endpoint + key),
  default model, GitHub token, and an expert tier of per-agent tuning knobs rendered
  from the backend's `/settings/schema`.
- **`ReposPage`** — the repo registry (see below).

**Secret UX pattern (shared by both pages).** API keys and tokens are **masked** by the
backend on read (`••••••••`). The input shows the mask; leaving it masked on save keeps
the stored secret, so editing a label never wipes a key. Clearing the field removes it.
The dashboard never sees a raw secret. Copy this pattern for any future secret field.

---

## Repo registry (the feature this session added — GH #41)

Previously a run was started by pasting a repo URL into the new-run form every time. Now
repositories are **first-class, registered once, and picked** on the run form.

**`ReposPage.tsx`** — add / list / edit / remove repos. Adding a repo **validates it
against the GitHub API first** (`POST /repos`): the backend canonicalizes the URL and
does a `GET /repos/{owner}/{repo}` with the resolved token, so a typo or a private repo
with no access is rejected *before* the entry is stored — no more failing halfway through
a clone. Each card carries its validation state on its **left edge** (green/red/grey) and
a matching pill (`● public`, `● private`, `✗ not found`), mirroring the run-box visual
language. "Check access" re-validates on demand.

**`NewRunPage.tsx`** — the repository field is now a **dropdown of registered repos**
(no free-text). Selecting one fetches its live branch list (`GET /repos/{id}/branches`)
into a **base-branch picker**; a branch-fetch failure degrades to "use the default
branch" with a warning rather than blocking the run. Starting the run preflights **both**
the repo and the LLM provider server-side — a 4xx surfaces inline and no run is created.

Token resolution is entirely server-side (per-repo token → global Settings token →
anonymous); the dashboard only ever sends/receives the mask. Full rationale in
`../docs/decisions/0024-repo-registry.md`.

---

## File map

```
src/
  main.tsx              React root (StrictMode) + global CSS imports
  App.tsx               the only "router": view union + runId; composes every screen
  api.ts                typed axios client — ONE interface per Pydantic model in
                        ../src/api/responses/responses.py. Keep the two in sync.
  defaultRunRequest.ts  builds a full RunRequest from the few fields the form collects
  eventInfo.ts          per-event-type labels/colors for the timeline
  styles.css            the whole design system (CSS custom props in :root)

  hooks/
    useRunStatus.ts     poll GET /runs/{id} → run_state + hitl_phase
    useLogStream.ts     SSE live logs, seq-deduped, active-gated

  components/
    RunsHome.tsx        home grid of past runs + pulse strip
    NewRunPage.tsx      trigger a run — repo + branch pickers, model/gate overrides
    ReposPage.tsx       repo registry CRUD (GH #41)
    SettingsPage.tsx    settings hub (providers, model, GitHub token, expert knobs)
    StatusBar.tsx       lifecycle badge + "now doing" live line
    PipelineGraph.tsx   SVG of the agent graph, active node/edge lit
    RunOverview.tsx     task, phase gantt, microtask board
    HitlPanel.tsx       the approval gate — fetch context, approve/reject(+note)
    PlanReview.tsx      plan + microtasks view (plan gate)
    DiffReview.tsx      green/red diff view (code gate), react-diff-view retheme
    LogPanel.tsx        live log feed (stick-to-bottom)
    EventTimeline.tsx   durable event history (poll by seq)
    ArtifactsPanel.tsx  plan/review/diff/test artifacts, bodies on demand
    LessonsPanel.tsx    per-repo cross-run lessons (ADR 0018)
```

---

## Working in this codebase

**The API client is the contract.** Every `interface` in `api.ts` mirrors a Pydantic
response model in `../src/api/responses/responses.py`. When the backend changes a shape,
update `api.ts` in the same change — that is what keeps the dashboard type-safe against
the server. The axios response interceptor normalizes every error to
`Error("<status> <detail>")`, pulling FastAPI's `detail` string, so component `catch`
blocks can just show `String(e)`.

**Adding a screen:** add a value to the `View` union in `App.tsx`, a topbar button, and a
full-page route line next to the Settings / Repos ones.

**Adding an endpoint:** add its `interface` + method to `api.ts`, and — if it's a new
top-level path prefix — add that prefix to `vite.config.ts` (see the warning above).

**Styling:** everything is in `styles.css` driven by the `:root` custom properties
(`--bg`, `--panel`, `--accent`, the per-agent `--cat-*` hues, …). Reuse the existing
component families (`.provcard`, `.runbox`, `.fld`, `.chip`, `.valpill`) rather than
inventing one-off styles — the visual consistency across screens is deliberate.

---

## Shipping (later)

`npm run build` → `dist/`, served by FastAPI `StaticFiles`, or wrapped in Tauri for a
desktop binary. Right now `dist/` is not yet mounted by the API; dev runs through the
Vite proxy.
