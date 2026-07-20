# SWE-bench harness for AgenticDevOps

Runs the real engine (`api.service.Service`) over a SWE-bench slice and produces a
`predictions.jsonl` the official [SWE-bench](https://github.com/SWE-bench/SWE-bench)
harness grades. See the module docstring in `adapter.py` for how a prediction is produced.

## Quickstart (wrapper)

`run_benchmark.sh` does the whole flow — infra up → migrations → deps → generate
predictions → (optional) grade — in one shot:

```bash
scripts/swebench/run_benchmark.sh --limit 20            # generate 20 predictions
scripts/swebench/run_benchmark.sh --limit 20 --grade    # ...and grade them
scripts/swebench/run_benchmark.sh --limit 20 --skip-infra   # infra already up/built
```

- `--grade` runs the official harness after generating (needs Docker + `swebench` installed).
- `--skip-infra` skips compose-up / migrations / sandbox-image build (fails loudly if the
  `agentic-sandbox:0.1` image is missing).
- Run-param overrides go **after a literal `--`** (see below).

The sections below run the two steps by hand — use them when iterating on one instance or
debugging.

## Prereqs

```bash
# 1. Infra (same as a normal run — see project CLAUDE.md)
docker compose -f deploy/docker-compose.yml up -d
docker compose -f deploy/docker-compose.yml --profile donotstart build sandbox-image-builder
venv/bin/alembic upgrade head

# 2. A working LLM provider configured in Settings (the run preflights it).
#    Optionally set a GitHub token in Settings to dodge public-repo rate limits.

# 3. Benchmark deps
venv/bin/pip install swebench datasets
```

## 1. Generate predictions (start SMALL)

```bash
cd src
../venv/bin/python ../scripts/swebench/adapter.py \
    --dataset princeton-nlp/SWE-bench_Verified \
    --limit 20 \
    --out ../predictions.jsonl
```

- `--limit 0` = all instances. `--offset N` skips the first N. `--instance <id>` runs one.
- Re-running appends; instances already in `--out` are skipped (resume-safe).
- `--max-tokens N` caps tokens per instance (default: no cap — set one, this can run long).
- Empty patch = the run FAILed / stalled before deploy → graded unresolved (expected).

### Overriding run params (model, caps) per run

By default a run uses whatever is configured in the **dashboard Settings** (DB). To override
per benchmark run without touching the dashboard, pass adapter flags (through the wrapper,
put them after a literal `--`):

```bash
# one model for every agent
scripts/swebench/run_benchmark.sh --limit 20 -- \
    --provider anthropic --model claude-opus-4-8

# cheap investigation, strong coder (per-agent flags win over the global one)
scripts/swebench/run_benchmark.sh --limit 20 -- \
    --provider anthropic --model claude-haiku-4-5-20251001 \
    --coder-model claude-opus-4-8

# raise the loop caps
scripts/swebench/run_benchmark.sh --limit 20 -- \
    --max-planner-reviewer-iterations 5 --max-microtask-attempts 4
```

- `--provider`/`--model` set all five agents; `--<agent>-provider`/`--<agent>-model`
  (planner/reviewer/coder/tester/reflector) override one.
- The **provider_id must already be configured in Settings** — that's where the base_url +
  API key live. The flags only pick among configured providers and swap the model string.
  The chosen (provider, model) pairs are health-checked before each run starts.
- No flags → the DB/global selection is used unchanged.
- `venv/bin/python scripts/swebench/adapter.py --help` lists every flag.

**Cost warning:** each instance runs the full planner→reviewer→coder→tester→reflector loop.
500 × that is a large spend. Prove the pipeline on 20, then Lite (300), then Verified (500).

## 2. Grade with the official harness

```bash
cd /home/kristian/project/AgenticDevOps
venv/bin/python -m swebench.harness.run_evaluation \
    --dataset_name princeton-nlp/SWE-bench_Verified \
    --predictions_path predictions.jsonl \
    --max_workers 8 \
    --run_id agenticdevops_v1
```

Needs Docker (the harness builds a per-instance image, applies our `model_patch`, and runs
the hidden FAIL_TO_PASS / PASS_TO_PASS tests). Results land in `evaluation_results/`.

## 3. Read the result

`--model-name` is `agenticdevops` (default), so the run writes:

- **`agenticdevops.<run_id>.json`** — the summary report (in the repo root). The number that
  matters is `resolved_instances` / `total_instances`; `resolved_ids` lists which passed.
- **`logs/run_evaluation/<run_id>/agenticdevops/<instance_id>/`** — per-instance detail:
  `report.json` (that instance's verdict), `patch.diff` (what we submitted), `test_output.txt`
  (the hidden tests running against it).

```bash
# score at a glance
cat agenticdevops.agenticdevops_v1.json | python -m json.tool | grep -E 'resolved|total'
```

An instance is **resolved** only when applying our patch flips its FAIL_TO_PASS tests to pass
AND keeps PASS_TO_PASS green — that's the SWE-bench score. Buckets in the report:

| bucket | meaning |
|---|---|
| `resolved_instances` | patch fixed it — **this is the score** |
| `unresolved_instances` | patch applied but hidden tests still fail |
| `error_instances` | patch didn't apply / harness error (often an empty patch) |
| `empty_patch_instances` | our run produced no diff (FAILed/stalled before deploy — see triage below) |

Our Tester's PASS/FAIL during the run is **irrelevant** to this number — the harness re-tests
from scratch in its own image. A green run can still be `unresolved` if the edit was wrong.

## Datasets

| id | size | use |
|---|---|---|
| `princeton-nlp/SWE-bench_Lite` | 300 | cheap smoke / iteration |
| `princeton-nlp/SWE-bench_Verified` | 500 | the headline number |
| `princeton-nlp/SWE-bench` | 2294 | full, expensive |

## Do NOT drive a benchmark run from the dashboard

The single-driver guard (`Service._active`, ADR 0032) is **process-local**. The adapter is
its own process, so if you also open the run in the dashboard and approve/resume it, both
processes drive the same `run_id` against one sandbox — the run gets torn down under itself
and can launder into a fake DONE (run b6487b43). While the adapter is running, the dashboard
is **view-only**: watch logs/events, never approve or resume. (A DB-backed cross-process
lock is the real fix — not done yet.)

## Empty patch → triage

An empty `model_patch` means the run FAILed/stalled before deploy. Common causes, all fixed
but worth checking if you see it again:

- **planner path typo** — planner emitted `target_files` without the `repo/` segment
  (`/workspace/astropy/...` instead of `/workspace/repo/astropy/...`); the coder read a
  ghost file, saw the real one compiled, and wrote nothing. Now re-rooted deterministically
  in `quality._normalize_output` + `coder_tools.to_sandbox_path`.
- **unbalanced quote in an investigation command** — `shlex.split` raised and killed the
  investigation. Now caught and fed back as an observation (planner/reviewer tools).
- **dead sandbox** — a torn-down sandbox used to read as "no output" and pass unverified;
  the tester now raises `SandboxError` → the run STALLs instead (recoverable via `/continue`).

Look at the coder debug file under `output/<ts>_<run_id>/implementing/` to see the actual
files the coder touched.

## Notes / limits

- The adapter uses `deploy_mode=dry_run` + a `deploy` HITL gate: the run pauses at deploy
  with its sandbox alive, we read `git diff --cached HEAD` (staged, so new files are
  included), then stop the run. Nothing is ever pushed to GitHub.
- Our Tester verdict does not affect scoring — the harness re-tests independently.
- Runs are sequential here (one at a time). The engine supports concurrent runs
  (ADR 0032); parallelising the batch is a possible follow-up but multiplies sandbox +
  LLM load.
