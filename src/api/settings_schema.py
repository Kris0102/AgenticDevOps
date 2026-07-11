"""Per-field metadata for the settings UI — one source of truth for labels + the info-button
descriptions (what each setting does). Served by GET /settings/schema; the form renders from
it. Keys are dotted paths into AppSettings. `group` drives the Basic / Expert split."""

SETTINGS_SCHEMA = [
    # ── Basic: per-agent model (GH #71) ─────────────────────────────────────────
    # The per-agent provider+model pickers are rendered directly by the Settings page
    # (selected.<agent>.provider_id / .model), not from this schema — there is no generic
    # dotted-field row for them. temperature/max_retries stay at their model defaults
    # (the agents run deterministic at 0); expose per-agent later if needed.

    # ── Expert · agent tuning (knobs.*) ─────────────────────────────────────────
    {"key": "knobs.planner_max_tool_steps", "label": "Planner tool steps", "type": "int", "group": "expert",
     "description": "Max tool-call rounds the Planner may take while investigating the repo. Higher = deeper reads but slower/costlier."},
    {"key": "knobs.planner_timeout_seconds", "label": "Planner timeout (s)", "type": "int", "group": "expert",
     "description": "Per-call LLM timeout for the Planner."},
    {"key": "knobs.planner_max_synthesis_attempts", "label": "Planner synthesis retries", "type": "int", "group": "expert",
     "description": "How many times the Planner re-tries producing a valid plan when the quality gate rejects it (feeding the error back)."},
    {"key": "knobs.reviewer_max_tool_steps", "label": "Reviewer tool steps", "type": "int", "group": "expert",
     "description": "Max tool-call rounds the Plan Reviewer may take investigating the repo before deciding approve/reject."},
    {"key": "knobs.reviewer_timeout_seconds", "label": "Reviewer timeout (s)", "type": "int", "group": "expert",
     "description": "Per-call LLM timeout for the Reviewer."},
    {"key": "knobs.coder_max_tool_steps", "label": "Coder tool steps", "type": "int", "group": "expert",
     "description": "Max tool-call rounds in the Coder's ReAct loop per microtask attempt."},
    {"key": "knobs.coder_timeout_seconds", "label": "Coder timeout (s)", "type": "int", "group": "expert",
     "description": "Per-call LLM timeout for the Coder (code generation is long — keep this generous)."},
    {"key": "knobs.coder_max_empty_diff_retries", "label": "Coder empty-diff retries", "type": "int", "group": "expert",
     "description": "Re-prompt attempts when the Coder's loop produces no file changes (empty diff)."},
    {"key": "knobs.coder_max_compile_retries", "label": "Coder compile retries", "type": "int", "group": "expert",
     "description": "Warm in-loop fix attempts when the coded target files don't compile, before handing to the Tester."},
    {"key": "knobs.tester_max_tool_steps", "label": "Tester tool steps", "type": "int", "group": "expert",
     "description": "Max tool-call rounds for the Tester's test-synthesis loop. Kept tight so a wandering synth fails fast."},
    {"key": "knobs.tester_timeout_seconds", "label": "Tester timeout (s)", "type": "int", "group": "expert",
     "description": "Per-call LLM timeout for the Tester."},
    {"key": "knobs.reflector_max_tool_steps", "label": "Reflector tool steps", "type": "int", "group": "expert",
     "description": "Max tool-call rounds the Reflector may take diagnosing a failed microtask."},
    {"key": "knobs.reflector_timeout_seconds", "label": "Reflector timeout (s)", "type": "int", "group": "expert",
     "description": "Per-call LLM timeout for the Reflector."},
    {"key": "knobs.reflector_max_test_resynth", "label": "Reflector test-resynth cap", "type": "int", "group": "expert",
     "description": "How many times the Reflector may send a wrong SYNTHESIZED test back to the Tester before forcing the code-fix path."},

    # ── Expert · run defaults (defaults.*) ──────────────────────────────────────
    {"key": "defaults.max_planner_reviewer_iterations", "label": "Max planner↔reviewer loops", "type": "int", "group": "expert",
     "description": "Hard cap on the planner↔reviewer PLANNING loop before a run is FAILED — stops a planner/reviewer that never converges. NOT a whole-run iteration budget; it only bounds the planning phase."},
    {"key": "defaults.max_microtask_attempts", "label": "Max microtask attempts", "type": "int", "group": "expert",
     "description": "How many coder↔tester↔reflector cycles ONE microtask may run before the run is FAILED — the per-microtask execution-loop cap. A microtask that can't pass its tests within this many attempts fails the run."},
    {"key": "defaults.max_total_tokens", "label": "Max tokens per run (thousands)", "type": "int", "group": "expert",
     "nullable": True, "scale": 1000,
     "description": "Hard cap on total tokens consumed across every agent in one run, entered in thousands "
                    "(e.g. 200 = 200,000 tokens). The run stalls once exceeded, recoverable via /continue. Blank = no cap."},
    {"key": "defaults.hitl_checkpoints", "label": "Default review gates", "type": "gates", "group": "expert",
     "description": "Which phases pause for human approval by default (plan / code / deploy). Overridable per run."},
    {"key": "defaults.teardown_on_terminal", "label": "Tear down sandbox when run ends", "type": "bool", "group": "expert",
     "description": "Destroy the run's Docker container once the run is terminal (completed or failed). A STALLED run always keeps its container so it can be continued."},
    {"key": "defaults.auto_create_pr", "label": "Auto-create pull request", "type": "bool", "group": "expert",
     "description": "After a successful deploy push, automatically open a GitHub pull request (pushed branch → the run's base branch) with a generated description (objective + microtasks + changed files). Needs a token with pull-request permission; a PR failure is logged in the run's events but never fails the run."},
]
