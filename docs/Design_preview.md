# Project Architecture & Design

<img src="../images/Agents_Brain.png" alt="Architecture diagram" width="400">

## 1. System Architecture

### 1.1 Multi-Agent Topology

We adopt a **Star topology**. Developers can specify custom interaction graphs, and many practical setups adopt a star topology in which a user proxy or manager agent delegates subtasks to tools and worker agents and then aggregates their outputs. This structure also supports human-in-the-loop oversight, which is critical for safety-sensitive applications.

- **Important Requirement:** We need a mechanism to detect which decisions of an agent are crucial for the system and spin up a human-in-the-loop instance.

![Multi-Agent Topology](../images/Mulit_Agent_Topology.png)

**Core decision:** All agent-to-agent communication is mediated by the **Orchestrator** via the **Blackboard (Run State)**. Agents do not directly message each other unless explicitly allowed by the interaction graph.

### 1.2 Hierarchical Verification

MAKER demonstrates that by decomposing tasks into granular steps and using distinct "Verifier" agents to challenge the output of "Worker" agents, systems can execute million-step reasoning chains with near-zero error accumulation. This aligns with findings from BOLAA and “Parenting” topologies (Supervisor/Child), where a Reviewer agent holds a distinct system prompt focused solely on critique and safety guidelines.

**=> Core decision:** We apply **hierarchical verification** at two critical boundaries:

1. **Planning Verification:** `Planner (Actor) -> Plan Reviewer (Critic)` loop until plan is approved or max iterations reached.
2. **Execution Verification (Grounded):** `Coder -> Tester (execution) -> Reflector/Reviewer` loop until tests pass or max attempts reached.

> Important: Code reviewers must be **grounded** by deterministic evidence (lint/test/build logs). Purely “static” LLM review is not sufficient.

---

### 1.3 Environment Capabilities

The agents’ capability to interact with the environment includes:

- Providing a sandbox environment within a docker container (executable scripts).
- API calls (via controlled tool gateway).
- Terminal use (via allowlisted commands and resource limits).

**=> Core decision:** All environment actions are performed through a **Tool Gateway** that enforces:

- allowlists/denylists
- timeouts
- CPU/memory limits
- restricted network egress
- auditing of every tool call into the event log

---

## 2. Agent Cognitive Architecture

Our agent’s decision core draws inspiration from the **CoALA (Cognitive Architectures for Language Agents)** framework.

![COALA Framework](../images/COALA_Framework_Cognitive_Architecture.png)

_(CoALA defines interacting modules (prompt templates, internal memories, retrieval, learning, grounding). A decision procedure executes in a loop using retrieval and reasoning to plan. While CoALA proposes Tree of Thoughts (ToT) and Monte Carlo Tree Search (MCTS), this simulation is **too complex** for our project.)_

### 2.1 Profiling & Roles

- Each agent has its own responsibility defined via a system prompt (e.g., “you are a coding agent”).
- We **WON’T** use dynamic roles.
- _(Optional)_ A pool of specialized agents can be selected depending on the task (UI agent, Backend agent, DevOps agent, etc.).

**=> Core decision:** Roles are **static** and enforced through:

- agent identity
- system prompt
- allowed tools per role (capability-based access control)

---

### 2.2 Agent Components: ($A = \langle \Phi, M, T, P \rangle$)

#### Perception

- Initially, we start with text-only input.
- _(Optional)_ Extend to image-based input later.

#### Memory Subsystem

- **Working Memory:** Current context window.
- **Episodic Memory:** Past run history + state + events stored in **Postgres**.
- **Semantic Memory:** Searchable long-term knowledge stored in a **vector database** (code embeddings + reusable reflections + SOPs).

**Retention Policy:**
![Retention Policy](../images/Retention_Retrieval_Memory_Policy.png)

**=> Core decision:** We minimize context pressure via:

- Microtask decomposition (small prompts)
- Retrieval on-demand (RAG)
- Storing large artifacts (logs/diffs) externally and referencing via artifact pointers

---

### 2.3 Action Space

- **Grounding Actions:** Execute external actions and process feedback into working memory as text.
- **Retrieval Actions:** Retrieve relevant code snippets/reflections via semantic memory.
- **Reasoning Actions:** Transform retrieved + observed context into decisions and next steps.
- **Learning Actions:**
  - We won’t fine-tune models.
  - We **CAN** update episodic memory with experience (events + state).
  - We **CAN** store reusable reflections/rules in semantic memory (after validation).

**=> Core decision:** “Learning” is implemented as **reflection distillation**:

- Reflector generates a candidate reusable rule (“Always normalize to UTC before comparing timestamps”)
- Reviewer (or policy gate) approves it
- Context Engine stores it in semantic memory under `chunk_type=reflection`

---

### 2.4 Planning

![Planning Strategies](../images/Planning_strategies.png)

**=> Core decision:** For simplicity, we will use **ReAct** (Reasoning + Act loop) within each microtask.

**Planning hierarchy:**

1. Planner creates overall plan and microtasks (Actor-Critic loop)
2. Orchestrator executes microtasks sequentially
3. Each microtask uses ReAct to retrieve context, act, and validate

---

### 2.5 Reflection & Self-Correction

To improve reliability, planners are complemented by reflection mechanisms (self-correction and verbal feedback methods such as Reflexion), allowing agents to critique and refine their plans and implementations based on evidence. Tools like Expel extract reusable rules from past mistakes and apply them to new tasks for episode-level repair and cross-episode generalization.

**=> Core decision:** Reflection is **evidence-driven**:

- Reflection is triggered on deterministic failures (lint/test/build/CI/security).
- Reflection output must reference artifacts (logs/diffs).
- Reusable reflections are stored only if they are reviewer-approved or repeatedly confirmed.

---

## 2.6 Used Patterns in MAS (Completed / Precise)

This section defines the **Multi-Agent System patterns** used in the project, why we use them, and where they appear in the pipeline.

> Each pattern includes: **Definition -> Where used -> Why it helps -> Implementation notes**.

### Pattern 1 — Role-Based Cooperation (RBAC + tool separation)

- **Definition:** Agents have fixed roles, each with a narrow toolset.
- **Where used:** All phases (planning/coding/testing/audit).
- **Why it helps:** Reduces tool hallucinations and prevents privilege escalation (e.g., Coder cannot deploy).
- **Implementation notes:**
  - Orchestrator enforces “allowed tools per agent”
  - Tool Gateway denies non-allowed commands and logs `tool.denied` events

---

### Pattern 2 — Actor–Critic (Planner <-> Plan Reviewer)

- **Definition:** One agent generates outputs (Actor), another critiques them (Critic) until convergence or stop.
- **Where used:** Planning phase, optional security exception reasoning.
- **Why it helps:** Raises plan completeness and reduces missing steps that cause downstream loops.
- **Implementation notes:**
  - Stop condition: max plan iterations (e.g., 3)
  - Reviewer must check: tests, migrations, rollback strategy, risk flags

---

### Pattern 3 — Grounded Verification Loop (Coder -> Tester -> Reflector)

- **Definition:** Code is validated by executing deterministic checks before critique.
- **Where used:** Microtask execution loop.
- **Why it helps:** Avoids “LLM sycophancy” and forces critiques to be evidence-based.
- **Implementation notes:**
  - Tester runs lint/typecheck/unit tests (and optionally integration)
  - Reflector uses logs + diff to propose minimal fix

---

### Pattern 4 — Microtask Queue (Task Decomposition + Sequential Execution)

- **Definition:** Break a large objective into ordered microtasks with explicit acceptance criteria.
- **Where used:** From planning through finalization.
- **Why it helps:** Keeps working memory small and makes progress measurable.
- **Implementation notes:**
  - Each microtask has: `acceptance_criteria`, `attempts`, `files_touched`
  - Orchestrator advances only when criteria pass (tests + checks)

---

### Pattern 5 — Tool Registry + Tool Gateway (Controlled tool-use)

- **Definition:** Tools are centrally registered and invoked through a policy layer.
- **Where used:** Everywhere tools are used (filesystem, git, test execution, search).
- **Why it helps:** Prevents unsafe shell usage and creates a complete audit trail.
- **Implementation notes:**
  - Allowlist commands (e.g., `pytest`, `rg`, `npm test`)
  - Enforce path restrictions (no reading secrets)
  - Enforce timeouts, resource limits, and network policies

---

### Pattern 6 — Retrieval-Augmented Generation (RAG) for Code & Reflections

- **Definition:** Agents retrieve relevant code chunks and past lessons from semantic memory.
- **Where used:** Planner (repo understanding), Coder (local edits), Reflector (bug patterns).
- **Why it helps:** Avoids dumping whole repos into prompts and improves correctness.
- **Implementation notes:**
  - Two semantic namespaces:
    - `baseline` (repo@base_sha)
    - `workspace` (run_id)
  - Retrieval is always filtered by namespace + filepath/language metadata

---

### Pattern 7 — Human-in-the-Loop (HITL) Gating (Critical Decisions)

- **Definition:** Certain actions require explicit human approval before proceeding.
- **Where used:** Plan approval (optional), PR creation (optional), merge, prod deploy, policy exceptions.
- **Why it helps:** Prevents unsafe autonomous merges/deployments and increases trust.
- **Implementation notes:**
  - HITL request is written as `hitl.requested` event
  - Decision written as `hitl.decided` event
  - Orchestrator blocks in `NEEDS_HITL_APPROVAL` state until resolved

---

### Pattern 8 — Cross-Reflection (Optional / Future Enhancement)

- **Definition:** Multiple reflectors propose fixes; a meta-critic selects the best.
- **Where used:** Only for hard failures (flaky tests, complex build errors).
- **Why it helps:** Reduces local minima and improves robustness on tricky bugs.
- **Implementation notes:**
  - Enable only when attempts exceed threshold (e.g., attempt ≥ 3)
  - Must still be grounded by logs

---

### Pattern 9 — Single-Path Plan Generator (Core for MVP)

- **Definition:** Planner produces one plan, improved by critique, rather than exploring many branches.
- **Where used:** MVP planning.
- **Why it helps:** Keeps orchestration simple while still gaining verification benefits.
- **Implementation notes:**
  - Avoid ToT/MCTS initially
  - Upgrade later if planning becomes a bottleneck

---

### Pattern 10 — Infinite Loop Recovery (Hard stop + escalation)

- **Definition:** Detect repeated failure cycles and stop or escalate.
- **Where used:** Plan loop and microtask loop.
- **Why it helps:** Prevents wasted compute/cost and stuck autonomous runs.
- **Implementation notes:**
  - Plan iterations max (e.g., 3)
  - Microtask attempts max (e.g., 5)
  - Global iterations max (e.g., 200)
  - Escalate to HITL with full evidence bundle

---

### Pattern 11 — Safety-by-Construction (Policy-first orchestration)

- **Definition:** Safety is enforced via deterministic constraints, not agent “good intentions.”
- **Where used:** Tool usage, secrets handling, deployment.
- **Why it helps:** Guarantees safety even when models hallucinate.
- **Implementation notes:**
  - deny-by-default network policy
  - forbidden paths list
  - secret scanning before PR/merge

---

## 3. Future Scope / Not Planned

- **Learning pipeline:** Not planned (fine-tuning is costly and high maintenance).
- **Error recovery:** Primary approach is grounded verification + reflection loops.
- **Infinite Loop recovery:** If loop iterates beyond thresholds, abort and demand HITL.
- **Self-learning Agents:** Agents may write reusable reflections to semantic memory after validation (no autonomous model updates).

---

## Knowledge Check

Is the pattern section clear enough that a new developer can map each pattern to a concrete pipeline phase and tool behavior? If you want, I can also add a **“pattern -> LangGraph node mapping”** table (Planner node, Reviewer node, Coder node, Tester node, Reflector node, HITL gate node).
