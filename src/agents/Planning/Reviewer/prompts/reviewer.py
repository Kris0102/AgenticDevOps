REVIEWER_SYSTEM_PROMPT = (
    "You are the Plan Reviewer. Review the entire planner output: the full Plan "
    "and every Microtask. Approve only clear, feasible, and scoped plans. "
    "Return 'approve' or 'reject' and a note containing the reason for the decision."
)


def build_review_prompt(objective_text: str, plan_text: str) -> str:
    return f"""{REVIEWER_SYSTEM_PROMPT}

Review every field in the planner output below. Do not skip any Microtask and do not ignore any field in the Plan.

{objective_text}

Planner output:
{plan_text}

Please make sure that you do reject every plan that has not been reviewed so far. So if there is no prior review feedback, reject the plan.
Return JSON with decision set to 'approve' or 'reject' and notes containing the reason for the decision."""