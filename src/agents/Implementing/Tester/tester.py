from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Sequence

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from agents.Implementing.Tester.prompt.tester_prompt import (
    TESTER_SYSTEM_PROMPT_JS,
    TESTER_SYSTEM_PROMPT_PYTHON,
    build_test_writing_prompt_js,
    build_test_writing_prompt_python,
)
from agents.Implementing.Tester.tester_output import create_test_file_as_output
from agents.Implementing.Tester.tester_tools import TesterTools
from observability import Category, get_logger
from orchestrator.state import BlackboardState, Microtask, RunStatus
from sandbox.Tools import Tools
from tool_gateway.Roles import Roles

load_dotenv()

_log = get_logger(Category.TESTER)

ProjectType = Literal["python", "javascript", "unknown"]


@dataclass(frozen=True)
class TestCheck:
    label: str
    command: List[str]


def _artifact_text(result: Dict[str, Any]) -> str:
    stdout = result.get("stdout", "")
    stderr = result.get("stderr", "")
    parts = [f"exit_code: {result.get('exit_code', 1)}"]
    if stdout:
        parts.append(f"STDOUT:\n{stdout}")
    if stderr:
        parts.append(f"STDERR:\n{stderr}")
    return "\n\n".join(parts)


def _test_file_path_python(microtask_id: str) -> str:
    safe_id = microtask_id.replace("-", "_")
    return f"tests/test_{safe_id}.py"


def _test_file_path_js(microtask_id: str) -> str:
    safe_id = microtask_id.replace("-", "_")
    return f"tests/test_{safe_id}.test.js"


def _max_tool_steps() -> int:
    raw = os.getenv("TESTER_MAX_TOOL_STEPS", "10")
    try:
        return max(1, int(raw))
    except ValueError:
        return 10


def _recursion_limit(max_tool_steps: int) -> int:
    return max_tool_steps * 2 + 5


class Tester:
    """Three-phase Tester:
    1. Detects project type (Python or JavaScript) from markers at repo root.
    2. LLM reads target source files and writes unit tests to a prescribed path.
    3. Deterministic test execution (pytest for Python, jest for JS) reports pass/fail.
    Unsupported project types are skipped without blocking the pipeline.
    """

    def __init__(self, run_id: str, tester_tools: TesterTools):
        self.run_id = run_id
        self.tester_tools = tester_tools
        self.gateway = tester_tools.gateway
        self.name = Roles.TESTER.value
        self.max_tool_steps = _max_tool_steps()

        api_key = os.getenv("CLUSTER_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("Missing API key. Set CLUSTER_API_KEY or OPENAI_API_KEY.")

        self.llm = ChatOpenAI(
            model=os.getenv("LLM_MODEL", "google/gemma-4-31B-it"),
            api_key=SecretStr(api_key),
            base_url="http://131.159.60.153:3000/inference",
            temperature=0.0,
            top_p=0.1,
            max_completion_tokens=4096,
            max_retries=int(os.getenv("LLM_RETRIES", "3")),
            timeout=int(os.getenv("TESTER_TIMEOUT_SECONDS", "120")),
        )
        self.python_agent = create_agent(
            self.llm,
            tools=self.tester_tools.build_tools(),
            system_prompt=TESTER_SYSTEM_PROMPT_PYTHON,
        )
        self.js_agent = create_agent(
            self.llm,
            tools=self.tester_tools.build_tools(),
            system_prompt=TESTER_SYSTEM_PROMPT_JS,
        )

    def invoke(
        self,
        state: BlackboardState,
        microtask: Microtask,
        final_validation: bool = False,
    ) -> Dict[str, Any]:
        rejection_note: str | None = state.get("test_rejection_note")
        _log.info(
            "tester.invoke.start",
            run_id=self.run_id,
            microtask_id=microtask.microtask_id,
            microtask_title=microtask.title,
            final_validation=final_validation,
            is_rewrite=bool(rejection_note),
        )

        project_type = self._detect_project_type()
        _log.info(
            "tester.project_type.detected",
            run_id=self.run_id,
            project_type=project_type,
            microtask_id=microtask.microtask_id,
        )

        if project_type == "unknown":
            return self._skip_unsupported(microtask, final_validation)
        if project_type == "javascript":
            return self._invoke_js(state, microtask, final_validation, rejection_note)
        return self._invoke_python(state, microtask, final_validation, rejection_note)

    # ------------------------------------------------------------------ #
    # Project type detection                                               #
    # ------------------------------------------------------------------ #

    def _detect_project_type(self) -> ProjectType:
        """Check repo root for well-known markers. JS (package.json) takes priority."""
        js_check = self.gateway.execute_tool(
            actor_role=Roles.TESTER,
            tool_name=Tools.SANDBOX_RUN,
            args={"cmd": ["find", ".", "-maxdepth", "1", "-name", "package.json"],
                  "cwd": "/workspace/repo", "timeout_s": 10},
        )
        if js_check.get("stdout", "").strip():
            return "javascript"

        py_check = self.gateway.execute_tool(
            actor_role=Roles.TESTER,
            tool_name=Tools.SANDBOX_RUN,
            args={"cmd": ["find", ".", "-maxdepth", "1",
                          "-name", "requirements.txt", "-o", "-name", "setup.py",
                          "-o", "-name", "pyproject.toml", "-o", "-name", "setup.cfg"],
                  "cwd": "/workspace/repo", "timeout_s": 10},
        )
        if py_check.get("stdout", "").strip():
            return "python"

        return "unknown"

    # ------------------------------------------------------------------ #
    # Python path                                                          #
    # ------------------------------------------------------------------ #

    def _invoke_python(
        self,
        state: BlackboardState,
        microtask: Microtask,
        final_validation: bool,
        rejection_note: str | None = None,
    ) -> Dict[str, Any]:
        test_file = _test_file_path_python(microtask.microtask_id)
        _log.info(
            "tester.write_tests.start",
            run_id=self.run_id,
            microtask_id=microtask.microtask_id,
            test_file=test_file,
            lang="python",
            is_rewrite=bool(rejection_note),
        )
        self._write_tests_python(state, microtask, test_file, rejection_note)
        _log.info("tester.write_tests.done", run_id=self.run_id, microtask_id=microtask.microtask_id, test_file=test_file)

        _log.debug("tester.install_deps.start", run_id=self.run_id, lang="python")
        self._install_python_dependencies()

        check = TestCheck(label="unit_tests", command=["python", "-m", "pytest", "-q", test_file])
        _log.info("tester.run_tests.start", run_id=self.run_id, microtask_id=microtask.microtask_id, command=check.command)
        result = self._run_check(check)

        test_content = self.tester_tools.read_sandbox_file(test_file)
        output_path = create_test_file_as_output(self.run_id, microtask, "python", test_file, test_content, result, rejection_note=rejection_note)

        return self._build_result(result, final_validation, output_path)

    def _write_tests_python(self, state: BlackboardState, microtask: Microtask, test_file: str, rejection_note: str | None = None) -> None:
        prompt = build_test_writing_prompt_python(
            objective=state.get("objective"),
            plan=state.get("plan"),
            microtask=microtask,
            test_file=test_file,
            rejection_note=rejection_note,
        )
        self.python_agent.invoke(
            {"messages": [HumanMessage(content=prompt)]},
            config={"recursion_limit": _recursion_limit(self.max_tool_steps)},
        )

    def _install_python_dependencies(self) -> None:
        """Best-effort: install the project's own deps so tests can import them."""
        self.gateway.execute_tool(
            actor_role=Roles.TESTER,
            tool_name=Tools.SANDBOX_RUN,
            args={"cmd": ["pip", "install", "--quiet", "pytest"], "cwd": "/workspace/repo", "timeout_s": 120},
        )

        candidates = [
            ["pip", "install", "-r", "requirements.txt"],
            ["pip", "install", "-r", "requirements-dev.txt"],
            ["pip", "install", "-r", "requirements/test.txt"],
        ]
        for cmd in candidates:
            result = self.gateway.execute_tool(
                actor_role=Roles.TESTER,
                tool_name=Tools.SANDBOX_RUN,
                args={"cmd": cmd, "cwd": "/workspace/repo", "timeout_s": 120},
            )
            if result.get("exit_code", 1) == 0:
                _log.info("tester.deps_installed", cmd=" ".join(cmd))

        has_package_def = self.gateway.execute_tool(
            actor_role=Roles.TESTER,
            tool_name=Tools.SANDBOX_RUN,
            args={"cmd": ["find", ".", "-maxdepth", "1", "-name", "pyproject.toml",
                          "-o", "-name", "setup.py", "-o", "-name", "setup.cfg"],
                  "cwd": "/workspace/repo", "timeout_s": 30},
        )
        if has_package_def.get("stdout", "").strip():
            result = self.gateway.execute_tool(
                actor_role=Roles.TESTER,
                tool_name=Tools.SANDBOX_RUN,
                args={"cmd": ["pip", "install", "-e", "."], "cwd": "/workspace/repo", "timeout_s": 120},
            )
            if result.get("exit_code", 1) == 0:
                _log.info("tester.editable_install_done")
            else:
                _log.debug("tester.editable_install_failed", stderr=result.get("stderr", ""))
        else:
            _log.debug("tester.editable_install_skipped", reason="no setup.py / pyproject.toml / setup.cfg found")

    # ------------------------------------------------------------------ #
    # JavaScript path                                                      #
    # ------------------------------------------------------------------ #

    def _invoke_js(
        self,
        state: BlackboardState,
        microtask: Microtask,
        final_validation: bool,
        rejection_note: str | None = None,
    ) -> Dict[str, Any]:
        test_file = _test_file_path_js(microtask.microtask_id)
        _log.info(
            "tester.write_tests.start",
            run_id=self.run_id,
            microtask_id=microtask.microtask_id,
            test_file=test_file,
            lang="javascript",
            is_rewrite=bool(rejection_note),
        )
        self._write_tests_js(state, microtask, test_file, rejection_note)
        _log.info("tester.write_tests.done", run_id=self.run_id, microtask_id=microtask.microtask_id, test_file=test_file)

        _log.debug("tester.install_deps.start", run_id=self.run_id, lang="javascript")
        self._install_js_dependencies()

        check = TestCheck(label="unit_tests", command=["npx", "jest", test_file, "--no-coverage"])
        _log.info("tester.run_tests.start", run_id=self.run_id, microtask_id=microtask.microtask_id, command=check.command)
        result = self._run_check(check)

        test_content = self.tester_tools.read_sandbox_file(test_file)
        output_path = create_test_file_as_output(self.run_id, microtask, "javascript", test_file, test_content, result, rejection_note=rejection_note)

        return self._build_result(result, final_validation, output_path)

    def _write_tests_js(self, state: BlackboardState, microtask: Microtask, test_file: str, rejection_note: str | None = None) -> None:
        prompt = build_test_writing_prompt_js(
            objective=state.get("objective"),
            plan=state.get("plan"),
            microtask=microtask,
            test_file=test_file,
            rejection_note=rejection_note,
        )
        self.js_agent.invoke(
            {"messages": [HumanMessage(content=prompt)]},
            config={"recursion_limit": _recursion_limit(self.max_tool_steps)},
        )

    def _install_js_dependencies(self) -> None:
        """Best-effort: run npm install, then ensure jest is available.
        Falls back to apt-get when the sandbox image predates the Node.js Dockerfile change.
        """
        # Detect missing npm before wasting time on npm install.
        npm_check = self.gateway.execute_tool(
            actor_role=Roles.TESTER,
            tool_name=Tools.SANDBOX_RUN,
            args={"cmd": ["npm", "--version"], "cwd": "/workspace/repo", "timeout_s": 10},
        )
        if npm_check.get("exit_code", 1) != 0:
            _log.warning("tester.npm_not_found",
                         reason="npm not in PATH — installing nodejs via apt-get (rebuild sandbox image to avoid this)")
            self._apt_install_nodejs()

        npm_result = self.gateway.execute_tool(
            actor_role=Roles.TESTER,
            tool_name=Tools.SANDBOX_RUN,
            args={"cmd": ["npm", "install"], "cwd": "/workspace/repo", "timeout_s": 180},
        )
        if npm_result.get("exit_code", 1) == 0:
            _log.info("tester.npm_install_done")
        else:
            _log.warning("tester.npm_install_failed",
                         stdout=npm_result.get("stdout", ""),
                         stderr=npm_result.get("stderr", ""),
                         exit_code=npm_result.get("exit_code"))

        jest_check = self.gateway.execute_tool(
            actor_role=Roles.TESTER,
            tool_name=Tools.SANDBOX_RUN,
            args={"cmd": ["npx", "jest", "--version"], "cwd": "/workspace/repo", "timeout_s": 60},
        )
        if jest_check.get("exit_code", 1) != 0:
            install_result = self.gateway.execute_tool(
                actor_role=Roles.TESTER,
                tool_name=Tools.SANDBOX_RUN,
                args={"cmd": ["npm", "install", "--save-dev", "jest"],
                      "cwd": "/workspace/repo", "timeout_s": 120},
            )
            if install_result.get("exit_code", 1) == 0:
                _log.info("tester.jest_installed")
            else:
                _log.warning("tester.jest_install_failed",
                             stdout=install_result.get("stdout", ""),
                             stderr=install_result.get("stderr", ""),
                             exit_code=install_result.get("exit_code"))

    def _apt_install_nodejs(self) -> None:
        """Install nodejs and npm via apt-get inside the running sandbox container."""
        self.gateway.execute_tool(
            actor_role=Roles.TESTER,
            tool_name=Tools.SANDBOX_RUN,
            args={"cmd": ["apt-get", "update", "-qq"], "cwd": "/workspace", "timeout_s": 60},
        )
        result = self.gateway.execute_tool(
            actor_role=Roles.TESTER,
            tool_name=Tools.SANDBOX_RUN,
            args={"cmd": ["apt-get", "install", "-y", "--no-install-recommends", "nodejs", "npm"],
                  "cwd": "/workspace", "timeout_s": 120},
        )
        if result.get("exit_code", 1) == 0:
            _log.info("tester.nodejs_apt_installed")
        else:
            _log.warning("tester.nodejs_apt_failed",
                         stdout=result.get("stdout", ""),
                         stderr=result.get("stderr", ""),
                         exit_code=result.get("exit_code"))

    # ------------------------------------------------------------------ #
    # Skip path                                                            #
    # ------------------------------------------------------------------ #

    def _skip_unsupported(self, microtask: Microtask, final_validation: bool) -> Dict[str, Any]:
        _log.info(
            "tester.skip_unsupported",
            run_id=self.run_id,
            microtask_id=microtask.microtask_id,
            reason="no package.json or Python marker found at repo root",
        )
        skipped_check: Dict[str, Any] = {
            "label": "unit_tests",
            "command": [],
            "status": "skipped",
            "exit_code": 0,
            "duration_ms": 0,
            "stdout": "",
            "stderr": "",
            "artifact_text": "exit_code: 0",
        }
        output_path = create_test_file_as_output(self.run_id, microtask, "unknown", "", "", skipped_check)
        next_status = RunStatus.DONE if final_validation else RunStatus.NEEDS_CODE
        _log.info(
            "tester.invoke.end",
            run_id=self.run_id,
            microtask_id=microtask.microtask_id,
            project_type="unknown",
            verdict="skipped",
            next_status=next_status.value,
        )
        return {
            "status": next_status,
            "repo_diff": self.tester_tools.get_repo_diff(),
            "check_results": [skipped_check],
            "summary": "Tests skipped: unsupported project type (not Python or JavaScript).",
            "final_validation": final_validation,
            "last_test_output_path": output_path or None,
            "test_rejection_note": None,
        }

    # ------------------------------------------------------------------ #
    # Shared helpers                                                       #
    # ------------------------------------------------------------------ #

    def _run_check(self, check: TestCheck) -> Dict[str, Any]:
        _log.debug("tester.run_check", run_id=self.run_id, label=check.label, command=check.command)
        result = self.gateway.execute_tool(
            actor_role=Roles.TESTER,
            tool_name=Tools.SANDBOX_RUN,
            args={"cmd": check.command, "cwd": "/workspace/repo", "timeout_s": 300},
        )
        exit_code = int(result.get("exit_code", 1))
        status = "passed" if "error" not in result and exit_code == 0 else "failed"
        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
        # Truncate in the log — full output goes to the on-disk tester output file.
        _log.debug(
            "tester.run_check.result",
            run_id=self.run_id,
            label=check.label,
            exit_code=exit_code,
            status=status,
            stdout_preview=stdout[:300] + ("…" if len(stdout) > 300 else ""),
            stderr_preview=stderr[:200] + ("…" if len(stderr) > 200 else ""),
        )
        _log.info(
            "tester.run_tests.done",
            run_id=self.run_id,
            label=check.label,
            status=status,
            exit_code=exit_code,
            duration_ms=int(result.get("duration_ms", 0)),
        )
        return {
            "label": check.label,
            "command": check.command,
            "status": status,
            "exit_code": exit_code,
            "duration_ms": int(result.get("duration_ms", 0)),
            "stdout": stdout,
            "stderr": stderr,
            "artifact_text": _artifact_text(result),
        }

    def _build_result(self, check_result: Dict[str, Any], final_validation: bool, output_path: str = "") -> Dict[str, Any]:
        all_passed = check_result["status"] == "passed"
        next_status = (
            RunStatus.DONE if all_passed and final_validation
            else RunStatus.NEEDS_CODE if all_passed
            else RunStatus.REFLECT
        )
        diff_text = self.tester_tools.get_repo_diff()
        _log.info(
            "tester.invoke.end",
            run_id=self.run_id,
            verdict="passed" if all_passed else "failed",
            next_status=next_status.value,
            final_validation=final_validation,
            output_path=output_path or None,
        )
        return {
            "status": next_status,
            "repo_diff": diff_text,
            "check_results": [check_result],
            "summary": self._build_summary([check_result], final_validation),
            "final_validation": final_validation,
            "last_test_output_path": output_path or None,
            # Clear the rejection note after use so it doesn't leak to the next microtask.
            "test_rejection_note": None,
        }

    @staticmethod
    def _build_summary(check_results: Sequence[Dict[str, Any]], final_validation: bool) -> str:
        passed = [item["label"] for item in check_results if item["status"] == "passed"]
        failed = [item["label"] for item in check_results if item["status"] == "failed"]
        skipped = [item["label"] for item in check_results if item["status"] == "skipped"]
        scope = "full regression" if final_validation else "microtask checks"
        if failed:
            return f"{scope} failed at {', '.join(failed)} after passing {', '.join(passed) if passed else 'no checks'}."
        if skipped:
            return f"{scope} skipped: {', '.join(skipped)}."
        return f"{scope} passed: {', '.join(passed)}."
