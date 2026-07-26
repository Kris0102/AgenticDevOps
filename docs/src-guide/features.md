# Feature Highlights — Demo-Worthy Capabilities

Patterns ([patterns.md](patterns.md)) are the *architecture* story; these are the
*"look what it actually does"* stories. Each entry: what it does, and the line that
makes it land in an interview.

---

## 1. Oscillation & stagnation detection (GH #61) — "it knows when it's stuck"

When the coder↔tester↔reflector loop stops making progress, the system doesn't burn
attempts until a dumb cap fires — it *proves* it's stuck and ends the run **BLOCKED**
with the actual conflict stated. Three layers, LLM judgment backstopped by determinism:

1. **Self-aware diagnosis:** the Reflector is shown its *own prior diagnoses* for the
   microtask and explicitly checks whether its next prescription undoes the previous
   one — flip-flopping ("remove `<=2`" → "re-insert `<=2`") is recognized as a
   requirements conflict, not a code bug.
2. **A `blocked` decision exists:** the Reflector can conclude "no code fix can
   satisfy both checks" — contradictory requirements end the run with the conflict
   named, instead of routing yet another doomed coder retry.
3. **Deterministic backstop (`_diff_cycle_kind`):** even if the LLM misses it, the
   orchestrator compares the per-attempt diff artifacts (whitespace-normalized):
   - `A→B→A` = **revert cycle** — each fix undoes the last; the checks demand
     mutually exclusive behavior.
   - `…B→B` = **stagnation** — the coder changed *nothing* despite fix instructions;
     re-running identical code against identical checks cannot pass.
   Needs ≥3 attempts, never fires on the first retry, and is suppressed while a
   `fix_target=test` re-synthesis still has budget (a correct "no code change" must
   not be misread as stagnation).

Result: "requirements conflict at X" on the dashboard instead of a mystifying
"attempts exhausted (3/3)". **Say:** "The system can distinguish *failing* from
*stuck* — and proves stuck from diff forensics, not vibes."

## 2. The system can conclude its own test is the bug

On a red test the Reflector routes `fix_target`: `code` (fix the implementation) or
`test` (the *synthesized test* asserts something wrong → the Tester re-synthesizes
it, bounded by a cap, counter reset when the coder actually runs). Without this, an
agent loop "fixes" correct code forever. **Say:** "Red doesn't always mean the code
is wrong — the MAS can indict its own test."

## 3. Token budget that stalls instead of kills — with a human top-up

Every agent's real usage is metered into a checkpointed run total. Hitting the cap
raises `TokenBudgetExceededError` → run goes **STALLED** (checkpoint + sandbox
intact), never FAILED. The dashboard shows a mandatory "tokens to add" input;
`POST /runs/{id}/continue` *adds* budget (validated — must exceed what's already
spent), emits a `run.token_budget_raised` audit event, and the run resumes exactly
where it stopped. **Say:** "Cost overrun is a pause-and-ask, not a crash — and the
raise itself is an audited event."

## 4. Live cost accounting that survives crashes

Dollar cost is a pure derivation over durable `llm.usage` events (written on every
path, including crashes) — per agent, per microtask, per model, with prompt-cache
read/write split. Models without a list price (self-hosted cluster) price to `None`
→ the total is a *flagged lower bound*, never a silently wrong number. **Say:** "We
can tell you what a run cost even if it died halfway — and we never fake a price."

## 5. Cross-run memory with human curation

Lessons are written only on *confirmed* outcomes (reviewer approve / tester pass)
and retrieved semantically into future runs on the same repo. On top: **operator
rules** — a human types a rule in the dashboard and it is *always* injected
(unranked, never loses a similarity race), displaced rules are visibly marked "not
sent to agents", and human-touched lessons are immune to bulk retraction. **Say:**
"The repo remembers what failed and why — and the human can pin house rules the
agents always see."

## 6. HITL gates that show the real thing

Gates at plan / code / deploy. The review context is genuine: the actual plan +
microtasks, the actual green/red diff, and at deploy the commit-message field is
**prefilled with the exact string that will be committed** (single code path for
prefill and push — they diverged once, never again). Reject *requires* a note, and
the note is injected into the same feedback channels agents already consume.
**Say:** "The human approves the artifact itself, not a paraphrase of it."

## 7. Four deploy modes, including a no-remote one

`dry_run` (see the diff, ship nothing) / `push` (run branch) / `pr` (push + real
GitHub PR with deterministic body; PR failure degrades, never fails the run) /
`local_only` — the sandbox commit is `git bundle`d and fetched **directly into your
local clone**, no remote involved at all. Default-branch push is refused at four
layers regardless of mode. **Say:** "You can run the whole MAS against a repo and
receive the result as a local branch without it ever touching GitHub."

## 8. Secret scanning gates every push

Gitleaks runs over the tree before anything leaves the sandbox; any finding
**hard-blocks** the push (`gitops.secret_blocked`, run FAILED). Plus deploy hygiene:
pathspec excludes keep dependencies, secrets, and the Tester's ephemeral synthesized
tests out of every commit. **Say:** "A leaked key fails the run — that's a
non-negotiable, not a warning."

## 9. Honest testing: UNVERIFIED beats false-FAIL

When nothing deterministic can be decided (harness can't test the target — e.g.
`.tsx` under pytest), the Tester refuses up-front (`synth_untestable_reason`) or
passes through loudly labelled **UNVERIFIED** — because a FAIL nothing can flip once
drove the reflector+coder loop to BLOCK correct code. Synthesized tests assert
structure only unless the Planner supplied a concrete IO example — the LLM never
invents expected values. The quality gate also rejects *tautological* acceptance
criteria ("all tests pass", "it works"). **Say:** "The verdict vocabulary includes
'I couldn't verify this' — pretending otherwise poisons the whole loop."

## 10. Coder compile gate with warm retries

After each diff the runner compiles the target files (`py_compile` /
`node --check`); errors are appended to the *same live ReAct conversation* for a
warm fix (no cold re-prompt, context preserved), bounded by a cap. Empty diffs
trigger a re-prompt too — "I changed the file" without a diff doesn't count.
**Say:** "Broken syntax never even reaches the Tester."

## 11. Full run control: stop, continue, delete, concurrent

Cooperative **stop** checked at the top of every node (bounds a live run to at most
one in-flight node — a sandbox kill alone can't reliably interrupt a ReAct loop);
crashes → STALLED and continuable from the checkpoint; DELETE only on terminal runs
(409 on live); multiple runs execute **concurrently** (pooled checkpointer
connections, atomic active-run registry) while each single run stays strictly
sequential. **Say:** "Every lifecycle transition a human might need exists, and none
of them can corrupt a run."

## 12. Preflight — a doomed run never starts

`POST /runs` health-checks the LLM provider (real call, also behind the settings
"Test connection" button) and validates repo, base branch, push branch, and
local-export path *before* creating anything. 4xx = no run row, no sandbox, no cost.
**Say:** "Misconfiguration fails in milliseconds at the API, not twenty minutes into
a run."

## 13. Ops surface: live SSE logs, seq-cursor history, provisioned Grafana

Two feeds by design: structlog → ring buffer → SSE for live tail; the `events` table
with a monotonic `seq` cursor for durable history. On top, a **fully provisioned
Grafana** (datasource + dashboards baked into the image) reads the same Postgres —
success rate, autonomy ratio, gateway denials, secrets blocked, self-correction
rate, cost per delivered microtask — pure SQL over the audit trail, zero extra
telemetry pipeline. **Say:** "Watch one run live, and interrogate the fleet's
history — same database, no new infrastructure."

## 14. Settings hub with encrypted secrets

Provider, model, and every knob are UI-managed (DB is source of truth, `.env` only
seeds first boot); per-run overrides resolve once at creation and are checkpointed.
API keys and the GitHub token are masked on read and Fernet-encrypted at rest — a DB
dump or snapshot leaks nothing. **Say:** "Change providers from the dashboard;
secrets never appear in plaintext, even in backups."

## 15. Semantic code search that stays fresh mid-run

The repo is AST-chunked and embedded locally (pgvector) at bootstrap; after every
coder diff the changed files are **re-synced** into a workspace namespace — so an
agent searching "where is the retry logic" finds the code *as it is right now in the
sandbox*, not as it was at clone time. **Say:** "Retrieval is kept honest while the
agents are actively rewriting what they search."

---

## If you only get to mention three

1. **Oscillation detection** (#1) — nobody expects a MAS to prove it's stuck via
   diff forensics and name the requirements conflict.
2. **Test self-indictment** (#2) — the loop can decide the test, not the code, is
   wrong. That's genuine self-correction.
3. **Stall-don't-fail token budget with audited top-ups** (#3) — turns the scariest
   autonomy question ("what does it cost?") into a controlled, human-gated knob.
