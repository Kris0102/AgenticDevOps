"""
sandbox_tools.py (PoC)

Implements the *data plane* tools for a Docker-per-run sandbox.

Key idea:
- Orchestrator NEVER shells out to git/docker directly.
- Orchestrator calls ToolGateway.call("sandbox.create"/"sandbox.exec"/"sandbox.read_file"...).
- ToolGateway enforces allowlists + writes audit events.
- This module implements the *actual tool logic*.

This PoC version:
- Uses Docker Engine via docker SDK.
- Runs git + tests INSIDE the container (git CLI, pytest).
- Captures stdout/stderr.
- Leaves artifacts persistence to the caller (ToolGateway), returning raw logs + optional refs.

Security note (PoC):
- This is minimal. For production, add stronger network isolation, seccomp/apparmor profiles,
  readonly rootfs options, and strict path validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Any, Tuple
import io
import os
import tarfile
import time
import uuid

import docker
from docker.models.containers import Container


# ----------------------------
# Types
# ----------------------------

@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int


@dataclass
class SandboxCreateResult:
    sandbox_id: str
    container_id: str


class SandboxError(RuntimeError):
    pass


# ----------------------------
# Sandbox Tool Implementation
# ----------------------------

class DockerSandboxTools:
    """
    A thin tool implementation layer over Docker.

    You can later expose these as MCP tools:
      - sandbox.create
      - sandbox.exec
      - sandbox.put_file
      - fs.read_file
      - fs.write_file
      - repo.diff
    """

    def __init__(
        self,
        image: str,
        workdir: str = "/workspace",
        cpu_quota: Optional[int] = None,
        mem_limit: Optional[str] = None,
        network_mode: Optional[str] = None,
        labels_base: Optional[Dict[str, str]] = None,
    ) -> None:
        self.image = image
        self.workdir = workdir
        self.cpu_quota = cpu_quota
        self.mem_limit = mem_limit
        self.network_mode = network_mode  # e.g. "none" for no network
        self.labels_base = labels_base or {}

        self.client = docker.from_env()

        # In-memory mapping for PoC. In production, store container_id in DB keyed by run_id.
        self._sandboxes: Dict[str, str] = {}

    # ---- lifecycle ----

    def sandbox_create(self, *, run_id: str) -> SandboxCreateResult:
        """
        Create a container per run. Return a sandbox_id to be used by other tool calls.
        """
        sandbox_id = f"sbox_{run_id}"
        if sandbox_id in self._sandboxes:
            # Idempotency for PoC
            cid = self._sandboxes[sandbox_id]
            return SandboxCreateResult(sandbox_id=sandbox_id, container_id=cid)

        labels = dict(self.labels_base)
        labels.update({"agentic.run_id": run_id, "agentic.sandbox_id": sandbox_id})

        container: Container = self.client.containers.run(
            self.image,
            command=["sleep", "infinity"],  # keep alive for exec calls
            detach=True,
            working_dir=self.workdir,
            labels=labels,
            network_mode=self.network_mode,
            mem_limit=self.mem_limit,
            cpu_quota=self.cpu_quota,
        )

        # Ensure working directory exists
        start = time.time()
        res = container.exec_run(
            cmd=["mkdir", "-p", self.workdir],
            workdir="/",
        )
        self._sandboxes[sandbox_id] = container.id
        return SandboxCreateResult(sandbox_id=sandbox_id, container_id=container.id)

    def sandbox_destroy(self, *, sandbox_id: str) -> None:
        cid = self._sandboxes.get(sandbox_id)
        if not cid:
            return
        c = self.client.containers.get(cid)
        try:
            c.kill()
        except Exception:
            pass
        try:
            c.remove(force=True)
        except Exception:
            pass
        self._sandboxes.pop(sandbox_id, None)

    # ---- core exec ----

    def sandbox_exec(
        self,
        *,
        sandbox_id: str,
        cmd: List[str],
        timeout_s: int,
        cwd: str,
        env: Optional[Dict[str, str]] = None,
    ) -> ExecResult:
        """
        Execute cmd (array form) inside container. No shell.
        """
        cid = self._require_container_id(sandbox_id)
        container = self.client.containers.get(cid)

        start = time.time()
        # docker SDK exec_run has a timeout only for API, not process kill.
        # For PoC: we rely on Docker not hanging; for production implement a watchdog.
        res = container.exec_run(
            cmd=cmd,
            workdir=cwd,
            environment=env,
            demux=True,  # returns (stdout, stderr)
        )
        duration_ms = int((time.time() - start) * 1000)

        exit_code = int(res.exit_code) if res.exit_code is not None else 1
        stdout_b, stderr_b = res.output if res.output is not None else (b"", b"")
        stdout = (stdout_b or b"").decode("utf-8", errors="replace")
        stderr = (stderr_b or b"").decode("utf-8", errors="replace")

        return ExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr, duration_ms=duration_ms)

    # ---- Git helpers (still using exec) ----

    def git_clone_and_checkout(
        self,
        *,
        sandbox_id: str,
        repo_url: str,
        base_sha: str,
        repo_dir: str = "repo",
        timeout_s: int = 600,
    ) -> None:
        """
        Clone and checkout inside the sandbox using git CLI.
        """
        # Clone
        clone = self.sandbox_exec(
            sandbox_id=sandbox_id,
            cmd=["git", "clone", repo_url, repo_dir],
            timeout_s=timeout_s,
            cwd=self.workdir,
        )
        if clone.exit_code != 0:
            raise SandboxError(f"git clone failed: {clone.stderr or clone.stdout}")

        # Checkout specific SHA
        checkout = self.sandbox_exec(
            sandbox_id=sandbox_id,
            cmd=["git", "checkout", base_sha],
            timeout_s=timeout_s,
            cwd=f"{self.workdir}/{repo_dir}",
        )
        if checkout.exit_code != 0:
            raise SandboxError(f"git checkout failed: {checkout.stderr or checkout.stdout}")

    def repo_diff(self, *, sandbox_id: str, repo_dir: str = "repo") -> str:
        diff = self.sandbox_exec(
            sandbox_id=sandbox_id,
            cmd=["git", "diff"],
            timeout_s=60,
            cwd=f"{self.workdir}/{repo_dir}",
        )
        # git diff exit code is usually 0 even with changes
        return diff.stdout

    # ---- File IO (PoC) ----

    def fs_read_file(self, *, sandbox_id: str, path: str) -> str:
        """
        Simple implementation: use `cat`. For production: use Docker archive APIs + strict path normalization.
        """
        self._validate_sandbox_path(path)
        res = self.sandbox_exec(
            sandbox_id=sandbox_id,
            cmd=["cat", path],
            timeout_s=30,
            cwd="/",
        )
        if res.exit_code != 0:
            raise SandboxError(f"read_file failed: {res.stderr or res.stdout}")
        return res.stdout

    def fs_write_file(self, *, sandbox_id: str, path: str, content: str) -> None:
        """
        Write a file using Docker's put_archive (safer than echo/heredoc).
        """
        self._validate_sandbox_path(path)
        cid = self._require_container_id(sandbox_id)
        container = self.client.containers.get(cid)

        # Build a tar archive in memory containing the file
        dir_name, file_name = os.path.split(path)
        data = content.encode("utf-8")

        tar_stream = io.BytesIO()
        with tarfile.open(fileobj=tar_stream, mode="w") as tar:
            ti = tarfile.TarInfo(name=file_name)
            ti.size = len(data)
            ti.mtime = int(time.time())
            tar.addfile(ti, io.BytesIO(data))
        tar_stream.seek(0)

        ok = container.put_archive(path=dir_name or "/", data=tar_stream.getvalue())
        if not ok:
            raise SandboxError("write_file failed: put_archive returned false")

    # ----------------------------
    # Internal helpers
    # ----------------------------

    def _require_container_id(self, sandbox_id: str) -> str:
        cid = self._sandboxes.get(sandbox_id)
        if not cid:
            raise SandboxError(f"Unknown sandbox_id: {sandbox_id}. Did you call sandbox.create?")
        return cid

    def _validate_sandbox_path(self, path: str) -> None:
        """
        Minimal path validation for PoC.
        Production: resolve realpath, enforce sandbox root, forbid .git, .env, secrets, etc.
        """
        if not path.startswith("/"):
            raise SandboxError("path must be absolute")
        if ".." in path.split("/"):
            raise SandboxError("path traversal is forbidden")
        # Optional: enforce everything under /workspace
        if not path.startswith(self.workdir + "/"):
            raise SandboxError(f"path must be under {self.workdir}/")