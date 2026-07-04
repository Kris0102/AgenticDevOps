"""LessonStore — durable, per-repo failure lessons (ADR 0018).

Cross-run learning: a confirmed failure lesson is written to the `lessons` table (embedded
for later semantic retrieval) so future runs on the SAME repo can be warned. Mirrors
EventLogger: `session.add`/`merge` only — the lending `session_scope()` owns the commit.

Write timing is OUTCOME-validated (ADR 0018): a lesson is stored at the CONFIRMING step, not
when the failure happens. The failing agent never blesses its own diagnosis:
  - plan rejections → stored when the reviewer APPROVES the revised plan.
  - reflector code diagnoses → stored when the microtask PASSES (the fix actually worked).

Best-effort: lessons are auxiliary — a failure to embed/store one never breaks the run.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from db.db import db
from db.models import Lesson
from db.semanticMemory.embedder import Embedder
from observability import Category, get_logger
from orchestrator.state import FeedbackDecision, Plan, Reflection

_log = get_logger(Category.SEMANTIC)


class LessonStore:
    @staticmethod
    def record_plan_lessons(
        session,
        run_id: str,
        repo_url: str,
        plan: Optional[Plan],
        agent: str,
    ) -> int:
        """On reviewer APPROVAL, persist the REJECT feedback the (now-approved) plan
        addressed as confirmed per-repo lessons. Returns the number stored."""
        if plan is None or not getattr(plan, "review_feedback", None):
            return 0
        rejects = [fb for fb in plan.review_feedback if fb.decision == FeedbackDecision.REJECT]
        stored = 0
        for idx, fb in enumerate(rejects):
            text = (fb.notes or "").strip()
            if not text:
                continue
            try:
                embedding = Embedder.instance().embed_query(text)
                session.merge(Lesson(
                    lesson_id=f"lesson_{run_id}_plan_{idx}",
                    repo_url=repo_url,
                    run_id=run_id,
                    agent=agent,
                    scope="plan",
                    failure_type=getattr(fb.source, "value", None),
                    summary=text,
                    guidance=None,
                    embedding=embedding,
                    created_at=datetime.now(timezone.utc),
                ))
                stored += 1
            except Exception as exc:  # never let lesson-keeping break the run
                _log.warning("lesson.store_failed", run_id=run_id, scope="plan", error=str(exc))
        if stored:
            _log.info("lesson.stored", run_id=run_id, scope="plan", count=stored, repo_url=repo_url)
        return stored

    @staticmethod
    def record_code_lessons( # This lesson is used after the tester confirms that the new implementation after the reflector gave feedback passes. This prevents faulty reflection from being added to the lessons!
        session,
        run_id: str,
        repo_url: str,
        reflection: Optional[Reflection],
        agent: str,
    ) -> int:
        """On microtask PASS, persist the reflector's CODE diagnosis that the now-green fix
        confirmed. The pass is the outcome-validation: the root_cause+fix demonstrably worked.

        Gated hard (ADR 0018): store ONLY a code-target diagnosis (`fix_target == "code"`) the
        reflector flagged reusable (`should_store_in_semantic_memory`) with a real root_cause.
        Test-target diagnoses ("the synthesized test was wrong") are NOT reusable code lessons —
        see `record_test_lessons`. Returns the number stored (0 or 1)."""
        if reflection is None:
            return 0
        if reflection.fix_target != "code" or not reflection.should_store_in_semantic_memory:
            return 0
        summary = (reflection.root_cause or "").strip()
        if not summary:
            return 0
        try:
            embedding = Embedder.instance().embed_query(summary)
            session.merge(Lesson(
                # One lesson per microtask: the latest confirmed diagnosis supersedes earlier
                # attempts on the same microtask (merge on the deterministic id).
                lesson_id=f"lesson_{run_id}_code_{reflection.microtask_id}",
                repo_url=repo_url,
                run_id=run_id,
                agent=agent,
                scope="code",
                failure_type=reflection.failure_type or None,
                summary=summary,
                guidance=(reflection.fix_instructions or "").strip() or None,
                embedding=embedding,
                created_at=datetime.now(timezone.utc),
            ))
        except Exception as exc:  # never let lesson-keeping break the run
            _log.warning("lesson.store_failed", run_id=run_id, scope="code", error=str(exc))
            return 0
        _log.info("lesson.stored", run_id=run_id, scope="code", count=1, repo_url=repo_url)
        return 1

    @staticmethod
    def retract_run(session, run_id: str) -> int:
        """Delete every lesson a run stored, called when that run FAILS. A lesson is only
        trustworthy if the run it came from actually worked out end-to-end: a run that
        fails may have "confirmed" a lesson at a phase boundary (reviewer approve / tester
        pass) that later proved confounded — the poison this guards against (ADR 0018 known
        limitation). A genuinely good lesson is re-learned by the next successful run.
        session.query(...).delete() only — the caller owns the commit."""
        deleted = (
            session.query(Lesson)
            .filter(Lesson.run_id == run_id)
            .delete(synchronize_session=False)
        )
        if deleted:
            _log.info("lesson.retracted", run_id=run_id, count=deleted, reason="run failed")
        return deleted

    @staticmethod
    def record_test_lessons() -> int:
        """No-op by design (ADR 0018). A `fix_target == "test"` diagnosis means the
        SYNTHESIZED test was wrong, not the repo's code — it is specific to a non-deterministic
        synthesis attempt, not a reusable per-repo lesson, so storing it would only poison
        future retrieval. Kept as an explicit anchor for the rejected alternative."""
        return 0

    # ------------------------------------------------------------------ #
    # Read path — semantic top-k retrieval (ADR 0018)                       #
    # ------------------------------------------------------------------ #

    @staticmethod
    def find_lessons(
        repo_url: str,
        query: str,
        *,
        scope: Optional[str] = None,
        top_k: int = 3,
        exclude_run_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Top-k semantically-closest lessons FOR THIS REPO. The DB does the ranking
        (pgvector cosine_distance → order_by → limit), so this stays O(top_k) no matter how
        many lessons accumulate — never load them all into a prompt. Scope-filterable
        (plan|code). Returns detached dicts (summary, guidance, scope, failure_type, score);
        empty on any failure — retrieval is advisory, never breaks the caller (ADR 0018).

        `exclude_run_id` drops lessons written by the CURRENT run. Lessons are the CROSS-run
        layer; the within-run layer is `state.reflections` (microtask-scoped). A same-run
        lesson points at code that is still changing in THIS run (higher staleness risk) and
        re-injecting it would create a within-run self-reinforcement loop — so the two layers
        are kept separate. Callers pass the live run_id."""
        text = (query or "").strip()
        if not text or not repo_url:
            return []
        try:
            qvec = Embedder.instance().embed_query(text)
            distance = Lesson.embedding.cosine_distance(qvec)
            with db.session_scope() as session:
                stmt = session.query(Lesson, distance.label("distance")).filter(
                    Lesson.repo_url == repo_url
                )
                if scope:
                    stmt = stmt.filter(Lesson.scope == scope)
                if exclude_run_id:
                    stmt = stmt.filter(Lesson.run_id != exclude_run_id)
                rows = stmt.order_by(distance).limit(top_k).all()
                return [
                    {
                        "summary": lesson.summary,
                        "guidance": lesson.guidance,
                        "scope": lesson.scope,
                        "failure_type": lesson.failure_type,
                        "score": round(1.0 - float(dist), 4),
                    }
                    for lesson, dist in rows
                ]
        except Exception as exc:  # retrieval is advisory — never break the caller
            _log.warning("lesson.retrieval_failed", repo_url=repo_url, scope=scope, error=str(exc))
            return []

    @staticmethod
    def list_for_repo(repo_url: str, limit: int = 100) -> List[Lesson]:
        """All lessons for a repo, newest first — the dashboard browse view ("what past
        runs taught the system"). Plain chronological listing, NOT semantic retrieval
        (that's find_lessons); detached rows via expunge so they outlive the session."""
        if not repo_url:
            return []
        with db.session_scope() as session:
            rows = (
                session.query(Lesson)
                .filter(Lesson.repo_url == repo_url)
                .order_by(Lesson.created_at.desc())
                .limit(limit)
                .all()
            )
            for row in rows:
                session.expunge(row)
            return rows

    @staticmethod
    def render_block(hits: List[Dict[str, Any]]) -> str:
        """Format retrieved lessons as an advisory prompt block. Empty string when there
        are none, so callers can concatenate unconditionally. Explicitly framed as past,
        possibly-stale notes — NEVER a hard rule (ADR 0018)."""
        if not hits:
            return ""
        lines = [
            "## Lessons from past runs on this repo (advisory — may be stale, not rules)",
        ]
        for h in hits:
            tag = f" [{h['failure_type']}]" if h.get("failure_type") else ""
            lines.append(f"- {h['summary']}{tag}")
            if h.get("guidance"):
                lines.append(f"  fix: {h['guidance']}")
        return "\n".join(lines)
