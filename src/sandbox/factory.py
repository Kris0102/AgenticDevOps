"""Single construction site for `DockerSandboxTools`.

Before this module, the image and workdir were hardcoded as string literals in ten places
(every node, the service, the deploy bridge, the benchmark adapter). That made a per-run
container shape impossible: a benchmark run needs a PRE-PROVISIONED image whose repo and
dependency environment are already baked in, while a normal dashboard run needs the generic
agent sandbox. Nodes now call `sandbox_for(state)` and the shape comes from the blackboard.

Read the spec from state; never re-derive it. A run's container shape is fixed at creation and
carried on the checkpoint, so every node in the run — and a resume days later — agrees on it.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from observability import Category, get_logger
from orchestrator.state import SandboxSpec
from sandbox.sandbox_tools import DockerSandboxTools

_log = get_logger(Category.SANDBOX)


def spec_from(state: Optional[Mapping[str, Any]]) -> SandboxSpec:
    """The run's `SandboxSpec`, or the defaults.

    Defaults are the historic hardcoded values (`agentic-sandbox:0.1` at `/workspace`, repo in
    `repo/`), so a checkpoint written before `sandbox_spec` existed — or any caller that has no
    state to hand — behaves exactly as it did before.
    """
    if state is None:
        return SandboxSpec()
    raw = state.get("sandbox_spec")
    if raw is None:
        return SandboxSpec()
    if isinstance(raw, SandboxSpec):
        return raw
    # LangGraph checkpoints round-trip through JSON, so a resumed run hands back a plain dict.
    if isinstance(raw, Mapping):
        try:
            return SandboxSpec(**raw)
        except Exception as exc:  # noqa: BLE001 — a malformed spec must not kill the run
            _log.warning("sandbox.spec_invalid", error=str(exc), fallback="SandboxSpec()")
    return SandboxSpec()


def sandbox_for(state: Optional[Mapping[str, Any]]) -> DockerSandboxTools:
    """`DockerSandboxTools` shaped by the run's `SandboxSpec`."""
    spec = spec_from(state)
    return DockerSandboxTools(
        image=spec.image,
        workdir=spec.workdir,
        repo_dir=spec.repo_dir,
        network_mode=spec.network_mode,
        path_prepend=spec.path_prepend,
    )


def repo_root(state: Optional[Mapping[str, Any]]) -> str:
    """Absolute path of the repo inside this run's container.

    THE source of that path for agent code. Every agent tool used to hardcode
    `/workspace/repo`, so a pre-provisioned image (repo at `/testbed`) made every command run
    in a directory that did not exist — exit 127 before any code was evaluated, reported as a
    broken environment rather than a wrong path. Nothing agent-side may hardcode it again.
    """
    return spec_from(state).workspace_path
