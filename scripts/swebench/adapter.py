"""SWE-bench adapter for AgenticDevOps.

Drives the SAME headless engine the dashboard uses (`api.service.Service`) over a slice
of a SWE-bench dataset, one instance per run, and emits a `predictions.jsonl` the official
SWE-bench harness can grade:

    python -m swebench.harness.run_evaluation \
        --dataset_name princeton-nlp/SWE-bench_Verified \
        --predictions_path predictions.jsonl \
        --max_workers 8 --run_id agenticdevops_v1

HOW A PREDICTION IS PRODUCED
----------------------------
Per instance we build a legacy `RunRequest` (raw `Repo`, no registry needed) pinned to the
instance's `base_commit`, then:
  * deploy_mode = DRY_RUN         — ship nothing; we only want the patch text.
  * hitl_checkpoints = [DEPLOY]   — the run drives to the deploy gate and PAUSES there.
    A pause is non-terminal, so `Service._finalize` does NOT tear the sandbox down
    (teardown only fires on terminal DONE/FAILED). The container is alive → we can read
    the diff out of it. We then stop the run to free the container.

The harness re-applies our `model_patch` inside ITS OWN per-instance image and runs the
hidden FAIL_TO_PASS / PASS_TO_PASS tests, so our own Tester verdict is irrelevant to the
score — we only need the diff. An instance that FAILs before reaching deploy (blocked
coder, plan cap, crash) yields an empty patch = graded unresolved, which is correct.

PATCH SEMANTICS
---------------
SWE-bench needs a unified diff relative to `base_commit`, INCLUDING new files. The engine's
own `git diff` (working tree) omits untracked files, so we stage first
(`git add -A` minus `.agentic_tests/`) and emit `git diff --cached HEAD`.

PREREQS
-------
  * Postgres up, migrations applied, sandbox image built (see CLAUDE.md "Run the pipeline").
  * A working LLM provider configured in Settings (the run preflights it).
  * `pip install swebench datasets` (datasets pulls the instances; swebench does grading).
  * Settings: turn `defaults.teardown_on_terminal` OFF is NOT required (we pause, not finish),
    but leave GitHub token set if you hit public-repo rate limits during clone/validate.

Run:
    cd src
    ../venv/bin/python ../scripts/swebench/adapter.py \
        --dataset princeton-nlp/SWE-bench_Verified --limit 20 --out ../predictions.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

import docker
from docker import errors as docker_errors

# This script lives in scripts/swebench/ but imports the app package under src/.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from api.requests.requests import RunRequest                       # noqa: E402
from api.responses.responses import RunState                       # noqa: E402
from api.service.service import Service                            # noqa: E402
from db.settings_store import SettingsStore                        # noqa: E402
from orchestrator.state import (                                   # noqa: E402
    Constraints,
    DeployMode,
    HITL,
    LLMConfig,
    Objective,
    Phase,
    Policy,
    Repo,
    Trigger,
)
from orchestrator.state import SandboxSpec                        # noqa: E402
from sandbox.sandbox_tools import DockerSandboxTools               # noqa: E402

_AGENTS = ("planner", "reviewer", "coder", "tester", "reflector")

# SWE-bench instance images check the repo out at /testbed (DOCKER_WORKDIR) with no nesting,
# and install its dependencies into a conda env rather than the system interpreter. Putting
# that env first on PATH is enough to make `python`/`pytest` resolve to it — no `conda
# activate` wrapper needed (verified against the real images).
_SWEBENCH_WORKDIR = "/testbed"
_SWEBENCH_PATH_PREPEND = "/opt/miniconda3/envs/testbed/bin"
# Docker Hub org publishing the per-instance images. Passed to swebench's own resolver so we
# never hand-roll its `__` → `_1776_` tag mangling.
_SWEBENCH_NAMESPACE = "swebench"


def _instance_image(instance: dict, namespace: str) -> str:
    """The official per-instance image name, from swebench's own resolver.

    Deliberately NOT reimplemented: the name embeds an arch, a tag, and a `__` → `_1776_`
    substitution that only exists for remote images. Deriving it here would silently drift
    from whatever the installed swebench version actually grades against.
    """
    from typing import Any, cast
    from swebench.harness.test_spec.test_spec import make_test_spec

    # `instance` is a plain dict at runtime; mypy/pyright expect a SWEbenchInstance
    # typed object. Cast to Any to satisfy the type checker while keeping runtime
    # behaviour unchanged.
    return make_test_spec(cast(Any, instance), namespace=namespace).instance_image_key


def _sandbox_spec(instance: dict, args: argparse.Namespace) -> Optional[SandboxSpec]:
    """Container shape for this instance, or None to use the generic agent sandbox.

    The generic sandbox is `python:3.14-slim` + pytest and has none of the instance's
    dependencies — nor even its Python version (SWE-bench targets 3.9), so a bootstrap
    `pip install` cannot fix it. Running inside the instance image is what lets the Tester
    actually EXECUTE the repo's suite instead of skipping it as unverifiable.
    """
    if args.generic_sandbox:
        return None
    return SandboxSpec(
        image=_instance_image(instance, args.sandbox_namespace),
        workdir=_SWEBENCH_WORKDIR,
        repo_dir="",                      # the image checks out straight into /testbed
        path_prepend=_SWEBENCH_PATH_PREPEND,
        network_mode=args.sandbox_network,
    )


def _ensure_image(image: str) -> None:
    """Make sure `image` is present locally, pulling once if not.

    These are ~4GB each, so this is the step that decides how far a batch can scale — prune
    between batches rather than assuming 500 instances will fit.
    """
    client = docker.from_env()
    try:
        client.images.get(image)
        return
    except docker_errors.ImageNotFound:
        pass
    print(f"  pulling {image} (~4GB, first time only) …", flush=True)
    client.images.pull(image)


def _objective(instance: dict) -> Objective:
    problem = (instance.get("problem_statement") or "").strip()
    title = problem.splitlines()[0][:120] if problem else instance["instance_id"]
    return Objective(
        title=title,
        description=problem,
        definition_of_done=[
            "Resolve the reported issue by editing the repository source.",
            "The repository's own test suite passes for the affected behaviour.",
        ],
        non_goals=[
            "Do not modify test files that encode the expected behaviour.",
            "Do not change unrelated modules.",
        ],
    )


def _build_llm_override(args: argparse.Namespace) -> Optional[LLMConfig]:
    """Per-run model selection from CLI flags, or None to keep the DB/global settings.

    `--provider`/`--model` apply to ALL five agents; a per-agent flag (`--coder-model`,
    `--planner-provider`, …) wins over the global one. Only fields the user actually set
    change — everything else keeps the DB's `settings.selected` value. The provider_id must
    reference a provider already configured in Settings (that's where base_url/api_key live —
    you configure providers once in the dashboard, this only picks among them + swaps the
    model string). The chosen (provider, model) pairs are preflighted before the run starts."""
    global_provider = args.provider
    global_model = args.model
    per_agent = {a: (getattr(args, f"{a}_provider"), getattr(args, f"{a}_model")) for a in _AGENTS}
    if not (global_provider or global_model or any(p or m for p, m in per_agent.values())):
        return None
    selected = SettingsStore.get().selected      # LLMConfig with all 5 agents from the DB
    updates = {}
    for agent in _AGENTS:
        cur = getattr(selected, agent)
        prov = per_agent[agent][0] or global_provider or cur.provider_id
        model = per_agent[agent][1] or global_model or cur.model
        updates[agent] = cur.model_copy(update={"provider_id": prov, "model": model})
    return selected.model_copy(update=updates)


def _run_request(instance: dict, args: argparse.Namespace) -> RunRequest:
    """A fully-autonomous, dry-run RunRequest pinned to the instance's base_commit,
    gated ONLY at deploy so the run pauses with its sandbox alive for diff extraction.
    Model selection + loop caps come from CLI flags (else DB/global settings)."""
    repo_slug = instance["repo"]                       # e.g. "django/django"
    return RunRequest(
        run_name=f"swebench:{instance['instance_id']}",
        objective=_objective(instance),
        trigger=Trigger(
            type="benchmark",
            source_url=f"https://github.com/{repo_slug}",
            actor="swebench-adapter",
        ),
        # Legacy raw-repo path — no registry entry needed. resolve_repo() still validates
        # it against GitHub and normalizes the URL. base_sha pins the exact commit.
        repo=Repo(
            repo_url=f"https://github.com/{repo_slug}",
            base_sha=instance["base_commit"],
            workspace_branch="agentic-devops-workspace",
        ),
        # None → resolve_run_config falls back to the global DB/env model selection.
        llm=_build_llm_override(args),
        constraints=Constraints(
            max_planner_reviewer_iterations=args.max_planner_reviewer_iterations,
            max_microtask_attempts=args.max_microtask_attempts,
            max_total_tokens=args.max_tokens,
            risk_level="low",
        ),
        policy=Policy(
            require_hitl_for_pr=False,
            require_hitl_for_merge=False,
            require_hitl_for_prod_deploy=False,
            security_scans_required=[],
            ci_required=False,
            protected_branches=[],
        ),
        # required=True is irrelevant to gating (hitl_checkpoints drives that); we gate at
        # DEPLOY only so the run pauses there with the sandbox intact.
        hitl=HITL(required=True, hitl_checkpoints=[Phase.DEPLOY]),
        deploy_mode=DeployMode.DRY_RUN,      # ship nothing — we only read the diff
        # The pre-provisioned instance image (unless --generic-sandbox). Bootstrap detects
        # the repo already present at /testbed and skips the clone.
        sandbox_spec=_sandbox_spec(instance, args),
    )


# Never part of a submitted patch: the Tester's ephemeral synth tests, and the .gitignore
# bootstrap may have seeded. Both are harness artefacts, not the model's solution.
_PATCH_EXCLUDES = (":(exclude).agentic_tests", ":(exclude).gitignore")


def _extract_patch(sandbox: DockerSandboxTools, run_id: str, workspace_path: str) -> str:
    """Unified diff vs the image/clone's HEAD, INCLUDING new files, read from the sandbox.

    Stage everything except our own artefacts, then diff the index against HEAD. `git add -A`
    picks up untracked new files that a plain `git diff` would miss — SWE-bench solutions
    often add files.

    On HEAD: a SWE-bench instance image sits ONE commit above `base_commit` (a build fix to
    pyproject.toml). Diffing against HEAD rather than `base_commit` is therefore both correct
    and preferable — the harness applies our patch onto that same image state, so the build-fix
    delta stays out of our patch instead of conflicting with it.
    """
    sandbox_id = sandbox.get_sandbox_id(run_id=run_id, verify=False)
    if not sandbox_id or not sandbox.sandbox_exists(
        sandbox_id=sandbox_id, start_if_created_but_not_running=True
    ):
        return ""   # run failed before/at bootstrap, or sandbox already gone → unresolved
    sandbox.sandbox_exec(
        sandbox_id=sandbox_id,
        cmd=["git", "add", "-A", "--", ".", *_PATCH_EXCLUDES],
        timeout_s=60,
        cwd=workspace_path,
    )
    res = sandbox.sandbox_exec(
        sandbox_id=sandbox_id,
        cmd=["git", "diff", "--cached", "HEAD", "--", ".", *_PATCH_EXCLUDES],
        timeout_s=60,
        cwd=workspace_path,
    )
    return res.stdout or ""


def _solve_one(service: Service, instance: dict, args: argparse.Namespace) -> dict:
    """Drive one instance to the deploy pause (or terminal), pull the patch, close the run."""
    model_name = args.model_name
    request = _run_request(instance, args)
    # Each instance has its OWN container shape (its own pre-provisioned image), so the
    # sandbox handle is built per instance rather than once for the batch.
    spec = request.sandbox_spec or SandboxSpec()
    _ensure_image(spec.image)
    sandbox = DockerSandboxTools(
        image=spec.image, workdir=spec.workdir, path_prepend=spec.path_prepend,
        network_mode=spec.network_mode,
    )
    # Same preflight the HTTP route does: validate repo + resolve LLM config.
    request = request.model_copy(update={"repo": service.resolve_repo(request)})
    config = service.resolve_and_preflight(request)
    run_id = str(uuid.uuid4())
    service.mark_active(run_id)              # claim before driving (mirrors the route)
    patch = ""
    try:
        response = service.run(request, run_id, config)   # blocks: drives to deploy pause / END
        # Same extraction either way: on a terminal-without-deploy the sandbox may still
        # exist (teardown_on_terminal off), and an empty patch is the correct answer if not.
        patch = _extract_patch(sandbox, run_id, spec.workspace_path)
    finally:
        # Close the run out so its container is freed regardless of outcome.
        try:
            if service.is_paused(run_id):
                service.stop_run(run_id, note="swebench: patch captured")
        except Exception as exc:               # noqa: BLE001 — best-effort cleanup
            print(f"  ! cleanup failed for {run_id}: {exc}", file=sys.stderr)
    return {
        "instance_id": instance["instance_id"],
        "model_name_or_path": model_name,
        "model_patch": patch,
    }


def _already_done(out_path: Path) -> set[str]:
    """instance_ids already present in the output file (resume support)."""
    if not out_path.exists():
        return set()
    done: set[str] = set()
    for line in out_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            done.add(json.loads(line)["instance_id"])
        except (json.JSONDecodeError, KeyError):
            continue
    return done


def main() -> None:
    ap = argparse.ArgumentParser(description="Run AgenticDevOps over a SWE-bench slice.")
    ap.add_argument("--dataset", default="princeton-nlp/SWE-bench_Verified",
                    help="HF dataset id (…/SWE-bench_Verified | _Lite | SWE-bench)")
    ap.add_argument("--split", default="test")
    ap.add_argument("--limit", type=int, default=20, help="number of instances (0 = all)")
    ap.add_argument("--offset", type=int, default=0, help="skip the first N instances")
    ap.add_argument("--instance", default=None, help="run a single instance_id and exit")
    ap.add_argument("--out", default="predictions.jsonl")
    ap.add_argument("--model-name", default="agenticdevops",
                    help="model_name_or_path recorded in each prediction")

    # --- run params (else the DB/dashboard settings decide) -------------------------
    g = ap.add_argument_group("run params (override the DB/dashboard settings per run)")
    g.add_argument("--provider", default=None,
                   help="provider_id for ALL agents (must be configured in Settings)")
    g.add_argument("--model", default=None, help="model for ALL agents")
    for _a in _AGENTS:
        g.add_argument(f"--{_a}-provider", default=None,
                       help=f"provider_id for the {_a} only (wins over --provider)")
        g.add_argument(f"--{_a}-model", default=None,
                       help=f"model for the {_a} only (wins over --model)")
    g.add_argument("--max-tokens", type=int, default=None,
                   help="per-instance total-token budget (default: no cap)")
    g.add_argument("--max-planner-reviewer-iterations", type=int, default=3,
                   help="cap on the planner↔reviewer loop (default 3)")
    g.add_argument("--max-microtask-attempts", type=int, default=3,
                   help="cap on the coder↔tester↔reflector cycle per microtask (default 3)")

    # --- sandbox (container the agents work in) --------------------------------------
    s = ap.add_argument_group("sandbox")
    s.add_argument("--generic-sandbox", action="store_true",
                   help="use the generic agentic-sandbox:0.1 instead of the per-instance "
                        "SWE-bench image. The repo's dependencies (and Python version) will "
                        "NOT be present, so the Tester cannot execute its suite — patches "
                        "still generate, but unverified. Mainly for comparison runs.")
    s.add_argument("--sandbox-namespace", default=_SWEBENCH_NAMESPACE,
                   help=f"Docker Hub org for the per-instance images (default "
                        f"{_SWEBENCH_NAMESPACE})")
    s.add_argument("--sandbox-network", default=None,
                   help="docker network mode for the sandbox: bridge (default) | none | host. "
                        "'none' is viable with a pre-provisioned image (no clone needed) and "
                        "denies the agents egress, but also breaks any test that installs or "
                        "downloads at runtime.")
    args = ap.parse_args()

    from datasets import load_dataset      # imported late so --help works without datasets

    ds = load_dataset(args.dataset, split=args.split)
    instances = list(ds)
    if args.instance:
        instances = [i for i in instances if i["instance_id"] == args.instance]
    else:
        instances = instances[args.offset:]
        if args.limit:
            instances = instances[: args.limit]

    out_path = Path(args.out)
    done = _already_done(out_path)
    if done:
        print(f"resume: {len(done)} instances already in {out_path}, skipping them")

    service = Service()

    total = len(instances)
    with out_path.open("a") as fh:
        for n, instance in enumerate(instances, 1):
            iid = instance["instance_id"]
            if iid in done:
                continue
            t0 = time.monotonic()
            print(f"[{n}/{total}] {iid} …", flush=True)
            try:
                pred = _solve_one(service, instance, args)
            except Exception as exc:            # noqa: BLE001 — never let one instance kill the batch
                print(f"  ! {iid} errored: {exc}", file=sys.stderr)
                pred = {"instance_id": iid, "model_name_or_path": args.model_name,
                        "model_patch": ""}
            fh.write(json.dumps(pred) + "\n")
            fh.flush()
            dt = time.monotonic() - t0
            size = len(pred["model_patch"])
            print(f"  → {'patch %d chars' % size if size else 'EMPTY patch'} ({dt:.0f}s)")

    print(f"done → {out_path}")


if __name__ == "__main__":
    main()
