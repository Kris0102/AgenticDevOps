#!/usr/bin/env bash
# Run the SWE-bench harness for AgenticDevOps end-to-end:
#   preflight → infra up → deps → generate predictions → (optional) grade.
#
# Does NOT need ./run.sh / uvicorn / the dashboard — the adapter imports the engine
# (api.service.Service) and drives the graph in-process. It only needs the SAME infra any
# run needs: Postgres, the sandbox image, and a reachable LLM provider.
#
# Usage:
#   scripts/swebench/run_benchmark.sh [--limit N] [--dataset ID] [--offset N]
#                                     [--instance ID] [--max-tokens N]
#                                     [--out FILE] [--grade] [--skip-infra]
#                                     [-- <extra adapter args>]
#
# Anything after a literal `--` is forwarded verbatim to adapter.py — that's how you set
# run params instead of the DB/dashboard settings, e.g. the model:
#   ... -- --provider anthropic --model claude-opus-4-8
#   ... -- --coder-model claude-opus-4-8 --tester-model claude-haiku-4-5-20251001
#   ... -- --max-planner-reviewer-iterations 5 --max-microtask-attempts 4
# (Run `venv/bin/python scripts/swebench/adapter.py --help` for the full list.)
#
# Examples:
#   scripts/swebench/run_benchmark.sh --limit 1                  # smoke: one instance
#   scripts/swebench/run_benchmark.sh --limit 20 --grade         # 20 + official grading
#   scripts/swebench/run_benchmark.sh --dataset princeton-nlp/SWE-bench_Lite --limit 300

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# ------------------------------------------------------------------ defaults / args
DATASET="princeton-nlp/SWE-bench_Verified"
LIMIT="20"
OFFSET="0"
INSTANCE=""
MAX_TOKENS=""
OUT="predictions.jsonl"
GRADE="0"
SKIP_INFRA="0"
RUN_ID="agenticdevops_$(date +%Y%m%d_%H%M%S)"

EXTRA=()          # forwarded verbatim to adapter.py (everything after a literal `--`)
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dataset)    DATASET="$2"; shift 2 ;;
    --limit)      LIMIT="$2"; shift 2 ;;
    --offset)     OFFSET="$2"; shift 2 ;;
    --instance)   INSTANCE="$2"; shift 2 ;;
    --max-tokens) MAX_TOKENS="$2"; shift 2 ;;
    --out)        OUT="$2"; shift 2 ;;
    --grade)      GRADE="1"; shift ;;
    --skip-infra) SKIP_INFRA="1"; shift ;;
    -h|--help)    sed -n '2,28p' "$0"; exit 0 ;;
    --)           shift; EXTRA=("$@"); break ;;
    *) echo "unknown arg: $1 (pass adapter flags after a literal --)" >&2; exit 2 ;;
  esac
done

PY="$ROOT_DIR/venv/bin/python"
PIP="$ROOT_DIR/venv/bin/pip"

die() { echo "ERROR: $*" >&2; exit 1; }
step() { echo ""; echo "==> $*"; }

# ------------------------------------------------------------------ preflight
step "Preflight"

[[ -x "$PY" ]] || die "venv missing ($PY). Run ./setup.sh first."
[[ -f .env ]]  || die ".env missing. Run ./setup.sh first."
docker info >/dev/null 2>&1 || die "Docker is not running. Start Docker and retry."

if docker compose version >/dev/null 2>&1; then DC=(docker compose); else DC=(docker-compose); fi
echo "  ok: venv, .env, docker"

# ------------------------------------------------------------------ infra
if [[ "$SKIP_INFRA" == "0" ]]; then
  step "Infra: Postgres up + sandbox image"
  "${DC[@]}" -f deploy/docker-compose.yml up -d postgres
  # Build the sandbox image only if it isn't present yet (slow; skip when cached).
  if ! docker image inspect agentic-sandbox:0.1 >/dev/null 2>&1; then
    echo "  building agentic-sandbox:0.1 (first time)…"
    "${DC[@]}" -f deploy/docker-compose.yml --profile donotstart build sandbox-image-builder
  else
    echo "  ok: agentic-sandbox:0.1 present"
  fi

  step "DB migrations"
  "$ROOT_DIR/venv/bin/alembic" upgrade head
else
  step "Infra: skipped (--skip-infra)"
  docker image inspect agentic-sandbox:0.1 >/dev/null 2>&1 \
    || die "sandbox image agentic-sandbox:0.1 missing — drop --skip-infra to build it"
fi

# ------------------------------------------------------------------ benchmark deps
step "Benchmark deps (swebench, datasets)"
if ! "$PY" -c "import datasets" >/dev/null 2>&1; then
  "$PIP" install --quiet datasets
fi
# swebench only needed for --grade, but cheap to ensure now.
if [[ "$GRADE" == "1" ]] && ! "$PY" -c "import swebench" >/dev/null 2>&1; then
  "$PIP" install --quiet swebench
fi
echo "  ok"

# ------------------------------------------------------------------ generate predictions
step "Generate predictions → $OUT"
echo "  dataset=$DATASET limit=$LIMIT offset=$OFFSET ${INSTANCE:+instance=$INSTANCE} ${MAX_TOKENS:+max_tokens=$MAX_TOKENS}"

ADAPTER_ARGS=(--dataset "$DATASET" --limit "$LIMIT" --offset "$OFFSET" --out "$ROOT_DIR/$OUT")
[[ -n "$INSTANCE" ]]   && ADAPTER_ARGS+=(--instance "$INSTANCE")
[[ -n "$MAX_TOKENS" ]] && ADAPTER_ARGS+=(--max-tokens "$MAX_TOKENS")
[[ ${#EXTRA[@]} -gt 0 ]] && ADAPTER_ARGS+=("${EXTRA[@]}")   # forwarded run-param flags

# Adapter imports the app package under src/ (adds it to sys.path itself). Run from repo root.
"$PY" scripts/swebench/adapter.py "${ADAPTER_ARGS[@]}"

# ------------------------------------------------------------------ grade (optional)
if [[ "$GRADE" == "1" ]]; then
  step "Grade with official SWE-bench harness (run_id=$RUN_ID)"
  "$PY" -m swebench.harness.run_evaluation \
    --dataset_name "$DATASET" \
    --predictions_path "$ROOT_DIR/$OUT" \
    --max_workers 8 \
    --run_id "$RUN_ID"
  echo ""
  echo "Grading done. Results in ./evaluation_results/ (run_id=$RUN_ID)."
else
  echo ""
  echo "Predictions written to $OUT. Grade them with:"
  echo "  $PY -m swebench.harness.run_evaluation \\"
  echo "    --dataset_name $DATASET --predictions_path $OUT --max_workers 8 --run_id $RUN_ID"
fi
