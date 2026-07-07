"""Per-field metadata for the settings UI — one source of truth for labels + the info-button
descriptions (what each setting does). Served by GET /settings/schema; the form renders from
it. Keys are dotted paths into AppSettings. `group` drives the Basic / Expert split."""

SETTINGS_SCHEMA = [
    # ── Basic ──────────────────────────────────────────────────────────────────
    {"key": "selected.provider_id", "label": "Provider", "type": "provider", "group": "basic",
     "description": "Which configured provider (endpoint + key) new runs use by default."},
    {"key": "selected.model", "label": "Model", "type": "string", "group": "basic",
     "description": "Default model id, e.g. google/gemma-4-31B-it or claude-sonnet-4. Pick from the provider's suggestions or type your own."},

    # ── Expert · generation ────────────────────────────────────────────────────
    {"key": "selected.temperature", "label": "Temperature", "type": "float", "group": "expert",
     "description": "Sampling randomness (0 = deterministic). The agents run at 0 for reproducible plans/code; raise only to explore."},
    {"key": "selected.max_retries", "label": "LLM retries", "type": "int", "group": "expert",
     "description": "How many times a single LLM call is retried on a transient error before it bubbles up (and stalls the run for /continue)."},

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
    {"key": "defaults.max_total_iterations", "label": "Max planner↔reviewer loops", "type": "int", "group": "expert",
     "description": "Hard cap on planning-loop iterations before a run is FAILED — stops a planner/reviewer that never converges."},
    {"key": "defaults.max_microtask_attempts", "label": "Max microtask attempts", "type": "int", "group": "expert",
     "description": "How many times one microtask may be retried (coder↔tester↔reflector) before the run is FAILED."},
    {"key": "defaults.max_total_tokens", "label": "Max tokens per run", "type": "int", "group": "expert", "nullable": True,
     "description": "Hard cap on total tokens consumed across every agent in one run. The run fails cleanly once exceeded. Blank = no cap."},
    {"key": "defaults.hitl_checkpoints", "label": "Default review gates", "type": "gates", "group": "expert",
     "description": "Which phases pause for human approval by default (plan / code / deploy). Overridable per run."},
    {"key": "defaults.teardown_on_terminal", "label": "Tear down sandbox when run ends", "type": "bool", "group": "expert",
     "description": "Destroy the run's Docker container once the run is terminal (completed or failed). A STALLED run always keeps its container so it can be continued."},
]
