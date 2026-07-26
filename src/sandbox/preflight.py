"""Image contract preflight.

The sandbox makes assumptions about its image that were free while the image was ours alone,
and become user-facing footguns the moment an arbitrary image can be named:

  * `timeout` accepting `--kill-after` — `sandbox_exec` wraps EVERY command in
    `timeout --kill-after=5 …` so a hanging command can't freeze a run.
  * `sh` — bootstrap seeds `.gitignore` via `sh -c '…'`.
  * `git` — clone, `repo.diff`, and patch extraction all shell out to git.
  * `sleep` — the container is kept alive with `sleep infinity`.
  * a usable working directory.

Without a preflight, a violation surfaces as a baffling mid-run failure long after the run was
accepted. With one it is a 422 at the moment the user types the name.

**Presence is not the contract; behaviour is.** busybox (Alpine) ships a `timeout` that exists
and then REJECTS `--kill-after`, so a presence-only check passes and every subsequent sandbox
command fails with exit 1 — precisely the failure this module exists to prevent. The timeout
check therefore runs the real invocation.

This deliberately does NOT go through `DockerSandboxTools`: that writes a `sandboxes` row and is
keyed by run_id, and a validation probe must leave no trace. It talks to Docker directly and
always removes its container.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterator, List, Optional
import uuid

import docker
from docker import errors as docker_errors

from observability import Category, get_logger

_log = get_logger(Category.SANDBOX)

# Programs that only need to EXIST. Value = why, surfaced to the user verbatim, because
# "missing sleep" is meaningless on its own.
REQUIRED_BINARIES: Dict[str, str] = {
    "sh": "bootstrap runs shell snippets via `sh -c` (.gitignore seeding)",
    "sleep": "the sandbox container is held open with `sleep infinity`",
    "git": "clone, `repo.diff`, and patch extraction all shell out to git",
}

# The exact wrapper `sandbox_exec` puts around every command. Checked by RUNNING it, not by
# looking for the binary: busybox has `timeout` but not `--kill-after`.
_TIMEOUT_PROBE = 'timeout --kill-after=5 2 sleep 0'
_TIMEOUT_REASON = ("every sandbox command is wrapped in `timeout --kill-after=5` so a hanging "
                   "command cannot freeze the run — install GNU coreutils (busybox's `timeout` "
                   "does not accept `--kill-after`)")

# Docker reports "image has no usable binaries" only when the container is STARTED, as an
# opaque APIError from runc. Recognising it lets us report the real cause (no shell/coreutils)
# instead of leaking an OCI stack trace to the user.
_MISSING_EXEC_MARKER = "executable file not found"

# Docker's per-layer status → the verb we show. Download reaches 100% long before the pull is
# finished (decompressing a 400MB image adds ~15s), so reporting only "downloading" leaves the
# bar pinned at 100% looking stuck. Anything unmapped keeps the previous verb.
# NB `Download complete` maps to "extracting", not "downloading": with least-advanced-wins
# (below), leaving a finished layer on "downloading" pins the overall verb there forever and
# "extracting" never shows — which is the whole point of this table. A layer past its download
# is, in aggregate terms, in the extract phase.
_STAGE_VERBS: Dict[str, str] = {
    "Pulling fs layer": "downloading",
    "Waiting": "downloading",
    "Downloading": "downloading",
    "Verifying Checksum": "verifying",
    "Download complete": "extracting",
    "Extracting": "extracting",
    "Pull complete": "extracting",
    "Already exists": "extracting",
}


@dataclass
class ImagePreflightResult:
    """Outcome of validating one image. `problems` are complete human-readable phrases."""
    ok: bool
    image: str
    pulled: bool = False
    problems: List[str] = field(default_factory=list)
    reason: Optional[str] = None

    def message(self) -> str:
        """One-line, actionable explanation — safe to hand straight to a 422 body."""
        if self.ok:
            return f"image '{self.image}' satisfies the sandbox contract"
        if self.problems:
            return (f"image '{self.image}' does not satisfy the sandbox contract: "
                    + "; ".join(self.problems))
        return f"image '{self.image}' is unusable as a sandbox: {self.reason}"


class PreflightPhase(str, Enum):
    """Where a streamed preflight has got to. `RESULT` is always the last event."""
    RESOLVING = "resolving"     # is the image already local?
    PULLING = "pulling"         # downloading layers — the slow one (GBs)
    CHECKING = "checking"       # container up, probing the contract
    RESULT = "result"           # terminal; carries the ImagePreflightResult


@dataclass
class PreflightProgress:
    """One streamed progress event.

    `percent` is only meaningful during PULLING, and only once Docker has reported layer
    totals — it is None until then rather than a fake 0, so a UI can show "starting…" instead
    of a bar that sits at zero and looks stuck.
    """
    phase: PreflightPhase
    detail: str
    percent: Optional[int] = None
    result: Optional[ImagePreflightResult] = None

    def as_event(self) -> dict:
        """JSON-safe shape for the SSE payload."""
        payload: dict = {"phase": self.phase.value, "detail": self.detail,
                         "percent": self.percent, "done": self.phase is PreflightPhase.RESULT}
        if self.result is not None:
            payload["ok"] = self.result.ok
            payload["message"] = self.result.message()
            payload["problems"] = self.result.problems
            payload["pulled"] = self.result.pulled
        return payload


class ImagePreflight:
    """Validate that an image can host a run. Pulls it if absent."""

    def __init__(self, client: Optional[docker.DockerClient] = None) -> None:
        self.client = client or docker.from_env()

    def check(self, image: str, *, workdir: str = "/workspace") -> ImagePreflightResult:
        """Blocking validation. Consumes `stream_check` so there is ONE implementation of the
        logic — a second copy would inevitably drift from the streamed one."""
        result: Optional[ImagePreflightResult] = None
        for progress in self.stream_check(image, workdir=workdir):
            if progress.result is not None:
                result = progress.result
        # stream_check always terminates with a RESULT event; this is belt-and-braces so a
        # future edit that breaks that invariant fails loudly instead of returning None.
        if result is None:      # pragma: no cover
            return self._fail(image, pulled=False,
                              reason="preflight produced no result (internal error)")
        return result

    def stream_check(self, image: str, *,
                     workdir: str = "/workspace") -> Iterator[PreflightProgress]:
        """Validate an image, yielding progress as it goes.

        Exists because pulling a multi-GB image can take minutes: without a progress channel the
        save request just blocks and the UI is indistinguishable from hung. The last event is
        always `PreflightPhase.RESULT`.
        """
        pulled = False
        try:
            yield PreflightProgress(PreflightPhase.RESOLVING, f"looking for {image} locally")
            if self._is_local(image):
                yield PreflightProgress(PreflightPhase.RESOLVING, "already present locally",
                                        percent=100)
            else:
                pulled = True
                yield from self._pull_with_progress(image)
        # NOTE ordering: ImageNotFound is a SUBCLASS of NotFound, and a bad *tag* on a real
        # repository raises plain NotFound — so catching only ImageNotFound sent bad tags down
        # the generic APIError path and leaked a raw HTTP 404 to the user.
        except docker_errors.NotFound:
            yield self._fail_event(
                image, pulled=False,
                reason="not found — no such repository or tag, locally or in the registry")
            return
        except docker_errors.APIError as exc:
            yield self._fail_event(image, pulled=False,
                                   reason=f"could not be pulled: {_docker_detail(exc)}")
            return

        yield PreflightProgress(PreflightPhase.CHECKING,
                                "starting a throwaway container to probe the image")
        yield self._probe(image, workdir=workdir, pulled=pulled)

    def _probe(self, image: str, *, workdir: str, pulled: bool) -> PreflightProgress:
        """Start a throwaway container and check the contract. Always removes the container."""
        name = f"preflight-{uuid.uuid4().hex[:12]}"
        container = None
        try:
            # create + start, NOT containers.run: `run` creates and then starts, and on a
            # start failure (an image with no `sleep` — runc refuses to exec) it raises
            # WITHOUT returning the handle, while the container it already created stays
            # behind. Splitting the two means `container` is always bound before anything can
            # fail, so `finally` can always clean up. Three orphans proved the point.
            container = self.client.containers.create(
                image, name=name, command=["sleep", "infinity"],
                # Same reason as sandbox_create: an image's own ENTRYPOINT would otherwise
                # swallow our keep-alive command and the container would exit at once.
                entrypoint=[], network_mode="none",   # a probe never needs egress
            )
            container.start()
            container.reload()
            if container.status != "running":
                logs = (container.logs() or b"").decode("utf-8", errors="replace").strip()
                return self._fail_event(
                    image, pulled=pulled,
                    reason=("the container exited immediately instead of staying up"
                            + (f" — {logs.splitlines()[-1][:160]}" if logs else "")))

            problems = self._contract_problems(container, workdir)
            if problems:
                return self._fail_event(image, pulled=pulled, problems=problems)

            _log.info("sandbox.image_preflight_ok", image=image, pulled=pulled, workdir=workdir)
            return PreflightProgress(
                PreflightPhase.RESULT, "image satisfies the sandbox contract", percent=100,
                result=ImagePreflightResult(ok=True, image=image, pulled=pulled))
        except docker_errors.APIError as exc:
            detail = _docker_detail(exc)
            # runc refuses to exec our keep-alive: the image has no `sleep`, which in practice
            # means no shell and no coreutils either. Report that as the contract violation it
            # is, rather than as an OCI stack trace nobody can act on. (`hello-world` and
            # scratch/distroless images land here.)
            # Match on the FULL exception text, not `detail` — the marker sits deep inside a
            # long runc trace and `_docker_detail` truncates before it.
            if _MISSING_EXEC_MARKER in str(exc):
                return self._fail_event(image, pulled=pulled, problems=[
                    f"`sleep` is missing ({REQUIRED_BINARIES['sleep']}) — the image could not "
                    "start even a keep-alive process, so it is almost certainly missing a shell "
                    "and coreutils entirely; the sandbox needs a general-purpose base image"])
            return self._fail_event(image, pulled=pulled,
                                    reason=f"could not start a container: {detail}")
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception as exc:      # noqa: BLE001 — a leaked probe must not fail the check
                    _log.warning("sandbox.preflight_cleanup_failed", container=name,
                                 error=str(exc))

    @staticmethod
    def _fail(image: str, *, pulled: bool, problems: Optional[List[str]] = None,
              reason: Optional[str] = None) -> ImagePreflightResult:
        """Build a failure result AND log it. Every failure path goes through here so a
        rejection always leaves a server-side trace — the 422 tells the user, this tells the
        operator. There is no durable `events` row: those are run-scoped, and a repo save has
        no run."""
        result = ImagePreflightResult(ok=False, image=image, pulled=pulled,
                                     problems=problems or [], reason=reason)
        _log.warning("sandbox.image_preflight_failed", image=image, pulled=pulled,
                     problems=problems or None, reason=reason)
        return result

    def _fail_event(self, image: str, *, pulled: bool, problems: Optional[List[str]] = None,
                    reason: Optional[str] = None) -> PreflightProgress:
        """Terminal failure event — logs via `_fail`, so streamed and blocking callers share
        one logging funnel."""
        result = self._fail(image, pulled=pulled, problems=problems, reason=reason)
        return PreflightProgress(PreflightPhase.RESULT, result.message(), result=result)

    # ------------------------------------------------------------------ internals

    def _is_local(self, image: str) -> bool:
        try:
            self.client.images.get(image)
            return True
        except docker_errors.ImageNotFound:
            return False

    def _pull_with_progress(self, image: str) -> Iterator[PreflightProgress]:
        """Pull `image`, yielding aggregate download progress.

        Docker reports progress PER LAYER, interleaved, with no overall total, so a percentage
        has to be aggregated. Two traps, both observed on a real 400MB pull:

        * **The denominator grows.** The first small layer can finish before later layers have
          even announced their size, so a naive sum hits 100% and then falls back to 20%. We
          therefore withhold the percentage until every announced layer has either reported a
          total or finished — reporting `None` (indeterminate) rather than a number that will
          regress.
        * **Monotonic clamp.** Belt and braces on top: a percentage never decreases, so any
          remaining accounting surprise shows as a stalled bar, never a rewinding one.

        `ImageNotFound` / `APIError` propagate to the caller's handlers.
        """
        repository, tag = _split_ref(image)
        _log.info("sandbox.image_pulling", image=image)
        totals: Dict[str, int] = {}
        current: Dict[str, int] = {}
        layer_stage: Dict[str, str] = {}
        announced: set = set()          # every layer id Docker has mentioned
        finished: set = set()           # layers Docker has finished with
        last_percent: Optional[int] = None
        last_stage = ""
        for event in self.client.api.pull(repository, tag=tag, stream=True, decode=True):
            # The daemon reports a failed pull in-band as well as by raising.
            if error := (event.get("error") or "").strip():
                raise docker_errors.APIError(error)
            layer, status = event.get("id"), (event.get("status") or "")
            detail = event.get("progressDetail") or {}
            # Only real LAYER events count. Docker's first line is
            # `{"status": "Pulling from library/python", "id": "3.12"}` — the id is the TAG, and
            # it never reports a size or a completion, so counting it as a layer left the
            # "still sizing" test permanently true and suppressed the percentage entirely.
            if layer and status in _STAGE_VERBS:
                announced.add(layer)
                if (total := detail.get("total")) and status == "Downloading":
                    totals[layer] = int(total)
                    current[layer] = int(detail.get("current") or 0)
                elif status in ("Download complete", "Pull complete", "Already exists"):
                    finished.add(layer)
                    if layer in totals:
                        current[layer] = totals[layer]
                layer_stage[layer] = _STAGE_VERBS[status]

            # Withhold a number while any announced layer has neither a size nor finished —
            # otherwise the denominator grows under us and the bar rewinds.
            sizing = any(l not in totals and l not in finished for l in announced)
            grand_total = sum(totals.values())
            percent = (None if sizing or not grand_total
                       else int(100 * sum(current.values()) / grand_total))
            if percent is not None and last_percent is not None:
                percent = max(percent, last_percent)      # never regress
            stage = _overall_stage(layer_stage) or last_stage or "downloading"
            # Emit on a whole-percent OR stage change: a big pull produces thousands of events
            # and forwarding all of them would flood the stream for no visible benefit.
            if percent != last_percent or stage != last_stage:
                if percent is not None:
                    last_percent = percent
                last_stage = stage
                size = f" ({_mb(grand_total)})" if grand_total else ""
                yield PreflightProgress(PreflightPhase.PULLING, f"{stage} {image}{size}",
                                        percent=percent)
        yield PreflightProgress(PreflightPhase.PULLING, f"pulled {image}", percent=100)

    def _contract_problems(self, container, workdir: str) -> List[str]:
        """Every contract violation, in one pass — so the user fixes the image once instead of
        discovering the next problem on the next rejected run."""
        problems: List[str] = []

        names = " ".join(REQUIRED_BINARIES)
        script = f'for b in {names}; do command -v "$b" >/dev/null 2>&1 || echo "$b"; done'
        res = container.exec_run(cmd=["sh", "-c", script])
        if res.exit_code != 0 and not res.output:
            # `sh` itself is unavailable, so nothing else could be probed either.
            return [f"`{b}` is missing ({why})" for b, why in REQUIRED_BINARIES.items()]
        out = (res.output or b"").decode("utf-8", errors="replace")
        absent = {ln.strip() for ln in out.splitlines() if ln.strip() in REQUIRED_BINARIES}
        # Declared order, so messages are stable rather than shell-dependent.
        problems += [f"`{b}` is missing ({REQUIRED_BINARIES[b]})"
                     for b in REQUIRED_BINARIES if b in absent]

        if container.exec_run(cmd=["sh", "-c", _TIMEOUT_PROBE]).exit_code != 0:
            problems.append(f"`timeout --kill-after=5` does not work ({_TIMEOUT_REASON})")

        if container.exec_run(
                cmd=["sh", "-c", f'mkdir -p "{workdir}" && test -d "{workdir}"']).exit_code != 0:
            problems.append(f"working directory '{workdir}' does not exist and cannot be created")

        return problems


# Least-advanced-wins order. Docker pulls layers CONCURRENTLY, so one can be extracting while
# another still downloads: deriving the label from the most recent event made it flap between
# verbs on almost every frame. The honest overall answer is the least-advanced stage — we are
# still "downloading" until every layer has finished downloading.
_STAGE_ORDER = ("downloading", "verifying", "extracting")


def _overall_stage(layer_stage: Dict[str, str]) -> str:
    """One stable verb for the whole pull, from the per-layer stages."""
    for stage in _STAGE_ORDER:
        if stage in layer_stage.values():
            return stage
    return ""


def _split_ref(image: str) -> tuple[str, str]:
    """`repository, tag` for the low-level pull API, which needs them separately.

    A registry host may carry a port (`host:5000/x`), so only a colon AFTER the last `/`
    is a tag separator. Digest refs (`repo@sha256:…`) are passed through whole.
    """
    if "@" in image:
        return image, ""
    tail = image.rsplit("/", 1)[-1]
    if ":" in tail:
        return image.rsplit(":", 1)[0], image.rsplit(":", 1)[1]
    return image, "latest"


def _mb(num_bytes: int) -> str:
    mb = num_bytes / (1024 * 1024)
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def _docker_detail(exc: Exception, limit: int = 200) -> str:
    """The human-usable part of a docker-py error.

    docker-py stringifies as
        `404 Client Error for http+docker://localhost/v1.55/images/create?…: Not Found ("<detail>")`
    and only `<detail>` means anything to a user. Surfacing the whole thing put an internal
    socket URL and an API version into a 422 body. Prefer the last parenthesised group; fall
    back to the text after the last `: ` before dumping the raw string.
    """
    raw = " ".join(str(exc).split())
    if not raw:
        return exc.__class__.__name__
    if raw.endswith('")') and '("' in raw:
        return raw[raw.rindex('("') + 2:-2][:limit]
    if raw.endswith(")") and "(" in raw:
        return raw[raw.rindex("(") + 1:-1][:limit]
    return raw.rsplit(": ", 1)[-1][:limit] if ": " in raw else raw[:limit]
