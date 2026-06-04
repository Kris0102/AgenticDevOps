## Goal

Planner "world interaction" goes **only** through the Tool Gateway with allowlisted, array-shaped commands — no free-form shell strings.

## Problem

`PlannerTools.execute_in_sandbox(command: str)` uses `shlex.split` and encourages open-ended shell in prompts. Docs forbid `sh -c`, pipes, and arbitrary commands.

## Tasks

- [ ] Replace string command tool with structured LangChain tools:
  - `read_file(path)` → `fs.read_file`
  - `run_command(cmd: list[str], cwd?, timeout_s?)` → `sandbox.run`
- [ ] Enforce `ROLE_PERMISSIONS` for `PLANNER`: `fs.read_file`, `sandbox.run` only
- [ ] Wire `check_requested_path` (or equivalent) on every `fs.read_file`
- [ ] Planner allowlist: discovery commands only (`ls`, `find`, `grep`, `cat`) — document in code
- [ ] Redact/truncate large tool outputs before returning to LLM context

## Acceptance criteria

- Disallowed tool (e.g. `fs.write_file` as Planner) returns `tool.denied` and is logged
- Disallowed command (e.g. `bash`, `curl`) is denied at gateway
- All successful calls emit `tool.executed` in Postgres `events` table

## References

- `docs/MCP_AND_TOOLS.md` §7
- `src/tool_gateway/gateway.py`
- `src/agents/Planning/planner_tools.py`

## Labels

`poc`, `planner`, `tool-gateway`
