# 0017 — Reflector reads tester output from the artifact, not a host file

Date: 2026-06-25
Status: Accepted

## Context
The Tester writes its full output to a host-filesystem file and put the **path** in the
blackboard (`state["last_test_output_path"]`). The Reflector then opened that file
(`_read_host_file`) to get the failing test output for diagnosis.

That is a side channel: two agents communicating through the local disk, which violates the
blackboard/star-topology invariant (agents communicate only via the blackboard). It is also
**not restart/relocation-safe** — the file lives on whatever host ran the Tester. The
checkpointed state survives a process restart; the file may not. The moment run execution
moves to a separate worker (planned, ADR 0013), the Reflector would silently read `""` and
diagnose blind.

Meanwhile the full output is **already** persisted durably: `TesterRunner._store_check_artifact`
writes it to the `artifacts` table, and the ref is already on the blackboard at
`evidence.checks.unit_tests.artifact_ref`.

## Decision
The Reflector reads the failing output from the **durable artifact**, not the host file:
- `ReflectorRunner._test_output(db_session)` (the **node** — it owns DB access) reads
  `evidence.checks.unit_tests.artifact_ref` → `db_session.get(Artifact, ref).content_text`.
- It passes the text into `Reflector.invoke(..., test_output=...)`; the **agent** just
  reasons over the string (no DB/filesystem access in the agent layer).
- The `unit_tests` artifact already aggregates every sub-check's output (VerdictPhase), so
  this is a single keyed read — no per-check iteration.

The on-disk tester output file stays, but only as a **human debug dump** — it is no longer
an agent-to-agent data channel. `last_test_output_path` remains for the HITL prompt.

## Consequences
- Respects the blackboard invariant: big content in the `artifacts` table, only the ref in
  state; the Reflector resolves the ref through the DB.
- Restart-/worker-safe: the artifact is in Postgres, not on a transient host disk.
- No new infrastructure — reuses the `artifacts` table the Tester already populates and the
  same `session.get(Artifact, ref)` read pattern used by `api/service/service.py`.
- Layer separation kept: DB access in the node, reasoning in the agent.

## Rejected alternatives
- **Keep the host file** — fragile side channel, silently breaks under a worker process.
- **Put the full output in `state`** — bloats every checkpoint row (the whole pytest
  traceback serialized every superstep); the lean ref+artifact split avoids it.
