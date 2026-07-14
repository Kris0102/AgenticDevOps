"""LessonStore — durable, per-repo failure lessons (ADR 0018) + human curation (ADR 0034).

Cross-run learning: a confirmed failure lesson is written to the `lessons` table (embedded
for later semantic retrieval) so future runs on the SAME repo can be warned.

Two write paths, two session conventions:
  - AGENT path (`record_*`, `retract_run`) runs inside a graph node that already holds a
    session, so it mirrors EventLogger: `session.add`/`merge`/`delete` only — the lending
    `session_scope()` owns the commit.
  - HUMAN path (`create_manual`/`update_lesson`/`delete_lesson`) is called from the API,
    where there is no ambient session, so it opens its own `session_scope()` — same as the
    read path below and as RepoStore.

Write timing is OUTCOME-validated (ADR 0018): a lesson is stored at the CONFIRMING step, not
when the failure happens. The failing agent never blesses its own diagnosis:
  - plan rejections → stored when the reviewer APPROVES the revised plan.
  - reflector code diagnoses → stored when the microtask PASSES (the fix actually worked).

Best-effort: lessons are auxiliary — a failure to embed/store one never breaks the run.
The human path is the exception: it raises, because a human typing a rule must be told
when it did not stick.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from db.db import db
from db.models import Lesson
from db.semanticMemory.embedder import Embedder
from observability import Category, get_logger
from orchestrator.state import FeedbackDecision, Plan, Reflection

_log = get_logger(Category.SEMANTIC)

# `agent` doubles as authorship. This value marks a lesson a human typed in the dashboard
# rather than one an agent learned — an "operator rule" (ADR 0034).
HUMAN_AGENT = "human"


class LessonNotFoundError(Exception):
    """Raised by the human CRUD path when a lesson_id does not exist."""
    def __init__(self, lesson_id: str) -> None:
        super().__init__(lesson_id)
        self.lesson_id = lesson_id


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
                microtask_id=reflection.microtask_id,
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
        session.query(...).delete() only — the caller owns the commit.

        Human-reviewed lessons are IMMUNE (ADR 0034): retraction exists because the AGENT's
        self-confirmation may be confounded, and a human who read and edited the lesson is a
        strictly stronger endorsement than the phase-boundary heuristic being distrusted here.
        Immunity is derived, not flagged — `updated_at` is only ever set by a human edit."""
        deleted = (
            session.query(Lesson)
            .filter(
                Lesson.run_id == run_id,
                Lesson.agent != HUMAN_AGENT,
                Lesson.updated_at.is_(None),
            )
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
    def find_operator_rules(
        repo_url: str,
        *,
        scope: Optional[str] = None,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """The human-authored rules for this repo — ALWAYS injected, never ranked (ADR 0034).

        Deliberately NOT a vector search: a rule a human typed is an instruction, not a
        retrieval candidate, and making it compete for a top-k slot against agent lessons
        would let a freshly-typed rule silently lose the cosine race and do nothing. Newest
        first, hard-capped so a growing rule list can never crowd out the prompt. Same dict
        shape as `find_lessons` (minus `score`) so `render_block` handles both. Advisory
        failure semantics: empty on error, never breaks the caller."""
        if not repo_url:
            return []
        try:
            with db.session_scope() as session:
                stmt = session.query(Lesson).filter(
                    Lesson.repo_url == repo_url,
                    Lesson.agent == HUMAN_AGENT,
                )
                if scope:
                    stmt = stmt.filter(Lesson.scope == scope)
                rows = stmt.order_by(Lesson.created_at.desc()).limit(limit).all()
                return [
                    {
                        "summary": row.summary,
                        "guidance": row.guidance,
                        "scope": row.scope,
                        "failure_type": row.failure_type,
                    }
                    for row in rows
                ]
        except Exception as exc:  # advisory — never break the caller
            _log.warning("lesson.rules_failed", repo_url=repo_url, scope=scope, error=str(exc))
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
    def render_block(
        hits: List[Dict[str, Any]],
        rules: Sequence[Dict[str, Any]] = (),
    ) -> str:
        """Format the repo's memory as a prompt block. Empty string when there is nothing,
        so callers can concatenate unconditionally.

        Two blocks with deliberately different force (ADR 0034): human-authored `rules` are
        instructions to follow, agent-learned `hits` stay framed as past, possibly-stale
        notes — NEVER hard rules (ADR 0018). Rules come first so they survive any
        downstream truncation."""
        if not hits and not rules:
            return ""
        lines: List[str] = []
        if rules:
            lines.append("## Operator rules for this repo (written by a human — follow these)")
            for r in rules:
                lines.append(f"- {r['summary']}")
                if r.get("guidance"):
                    lines.append(f"  how: {r['guidance']}")
            if hits:
                lines.append("")
        if hits:
            lines.append("## Lessons from past runs on this repo (advisory — may be stale, not rules)")
            for h in hits:
                tag = f" [{h['failure_type']}]" if h.get("failure_type") else ""
                lines.append(f"- {h['summary']}{tag}")
                if h.get("guidance"):
                    lines.append(f"  fix: {h['guidance']}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Human curation — the dashboard's lesson store (ADR 0034)             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def create_manual(
        repo_url: str,
        *,
        scope: str,
        summary: str,
        guidance: Optional[str] = None,
        failure_type: Optional[str] = None,
    ) -> Lesson:
        """Store an operator rule a human typed. `agent=HUMAN_AGENT` + `run_id=None` mark it
        as human-authored — the two facts every downstream derivation (pinning, retraction
        immunity, the dashboard's provenance line) reads.

        Embedded like any other lesson even though pinning means retrieval never ranks it:
        the column is NOT NULL, and an unpinned future would otherwise need a backfill.
        Unlike the agent path this RAISES on failure — a human must be told their rule did
        not stick."""
        text = (summary or "").strip()
        if not text:
            raise ValueError("summary is required")
        if not repo_url:
            raise ValueError("repo_url is required")
        embedding = Embedder.instance().embed_query(text)
        lesson = Lesson(
            lesson_id=f"lesson_human_{uuid.uuid4().hex[:12]}",
            repo_url=repo_url,
            run_id=None,
            agent=HUMAN_AGENT,
            scope=scope,
            microtask_id=None,
            failure_type=(failure_type or "").strip() or None,
            summary=text,
            guidance=(guidance or "").strip() or None,
            embedding=embedding,
            created_at=datetime.now(timezone.utc),
            updated_at=None,
        )
        with db.session_scope() as session:
            session.add(lesson)
            session.flush()
            session.expunge(lesson)
        _log.info("lesson.created", repo_url=repo_url, scope=scope, lesson_id=lesson.lesson_id)
        return lesson

    @staticmethod
    def update_lesson(
        lesson_id: str,
        *,
        summary: Optional[str] = None,
        guidance: Optional[str] = None,
        failure_type: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> Lesson:
        """Edit a lesson (agent-learned or human-authored) from the dashboard. Only the
        fields passed are touched; '' clears the nullable ones.

        Two side effects that matter: the embedding is REGENERATED when the summary
        changes (a stale vector would make the lesson unfindable by its own text), and
        `updated_at` is stamped — which is what makes the lesson immune to `retract_run`
        and shows the "edited by human" badge. Both derive from this single write."""
        with db.session_scope() as session:
            row = session.get(Lesson, lesson_id)
            if row is None:
                raise LessonNotFoundError(lesson_id)
            if summary is not None:
                text = summary.strip()
                if not text:
                    raise ValueError("summary cannot be empty")
                if text != row.summary:
                    row.embedding = Embedder.instance().embed_query(text)
                    row.summary = text
            if guidance is not None:
                row.guidance = guidance.strip() or None
            if failure_type is not None:
                row.failure_type = failure_type.strip() or None
            if scope is not None:
                row.scope = scope
            row.updated_at = datetime.now(timezone.utc)
            session.flush()
            session.expunge(row)
        _log.info("lesson.updated", lesson_id=lesson_id)
        return row

    @staticmethod
    def delete_lesson(lesson_id: str) -> None:
        """Permanently forget a lesson. The dashboard's escape hatch for a lesson that is
        wrong, stale, or was confirmed by a fluke — it stops being injected immediately."""
        with db.session_scope() as session:
            row = session.get(Lesson, lesson_id)
            if row is None:
                raise LessonNotFoundError(lesson_id)
            session.delete(row)
        _log.info("lesson.deleted", lesson_id=lesson_id)
