# Agent generation engine: audit, architecture and plan

## 1. Audit of the current project (commit 258bbce)

| Area | What exists | Verdict |
|---|---|---|
| Data layer (`store.py`) | SQLite, Postgres-portable SQL. Customers, requirements, versioned specs, test runs, runs, audit log, outputs. Sandbox copies for tests. | **Keep.** Sound pattern; extend with new tables. |
| Runtime (`runtime.py`) | Generic Claude tool loop, approval hook, audit log, refusal fallback, prompt caching, tools limited to the spec. | **Keep the loop.** Approvals are synchronous only (a person at a terminal), there is no token budget, and the turn limit is global. |
| Spec (`spec.py`) | Name, purpose, instructions, tools, restrictions, success criteria, test cases, open questions. | **Too thin.** No data sources, integrations, triggers, memory policy, workflow, limits, or risk. Tool config is untyped key/value. |
| Components (`components.py`) | 5 tools: knowledge search, draft email, send email, create record, escalate. | **The main limitation.** Two of five are email. No data querying, no external APIs, no memory, no scheduling. Every agent the designer can produce is a variant of an inbox assistant. |
| Designer (`designer.py`) | One call: notes → spec. Validates component names. | **Too direct.** It jumps from notes to a spec. It doesn't ask questions, analyse the process, assess feasibility or cost, or flag missing integrations before designing. |
| Evaluation (`evaluate.py`) | Sandbox run per case, tool check plus a grader model, 90% gate, stored per version. | **Keep the core.** Missing: test categories, failure injection (API outages), external-API fixtures, regression against earlier versions, cost checks, and a rule that safety cases must all pass. |
| Deployment | Only versions that passed can be deployed; rollback = deploy an older passed version. | **Keep.** Missing: a recorded human approval for agents that can change records, communicate externally, or move money. |
| Triggers | None; agents only run when someone types to them. | **Missing.** Scheduled and event-triggered work is required for operations, BI, and finance agents. |
| Monitoring | `usage` command: run count and tokens. | **Minimal.** No error/escalation rates, cost in money, approval statistics, or per-version comparison. |
| Security | Every row is scoped to an agent; tools limited to the spec; sensitive tools need approval. | **Partial.** No customer-scoped data files, no secret handling for external systems, no read-only enforcement for data access, no user roles. |
| Demo page (artifact) | Browser re-implementation of the v1 flow for a quick trial. | **Out of date and email-centric.** It duplicates logic in JavaScript. Replace it with a dashboard on top of the real engine (stage 2), rather than extending it. |
| Example | Accounting firm inbox. | Keep as *one* example. Add examples that aren't email: request routing using company data, and a scheduled business report. |

**Summary:** the plumbing is reusable (versioned specs in a database, a policy-enforcing tool loop, sandbox tests, gated deployment). The capability layer and the front of the pipeline are too narrow, so the system can only produce email-shaped agents.

## 2. Architecture

The central idea is that **an agent is data, not code**. A generated agent is a versioned, declarative spec. A generic runtime executes it using a library of tested capabilities. New agent types come from new *combinations and configurations* of capabilities, not new code paths. Custom code is the exception, and is added to the library as a reviewed component so the next customer can reuse it.

```
                ┌───────────────────── Requirements pipeline ─────────────────────┐
 company  ──►   intake ─► discovery questions ─► analysis ─► design ─► validate/assemble
 notes          (stored)   (answered by customer)  (opportunities,     (spec v N,       (risk policy,
                                                    integrations,       tests incl.      missing
                                                    feasibility, cost)  safety+failure)  connectors)
                └──────────────────────────────────────────────────────────────────┘
                                              │
                                              ▼
      ┌──────────── Evaluation ────────────┐     ┌──── Release ────┐     ┌──────── Operation ─────────┐
      │ sandbox copy of data               │     │ human approval  │     │ triggers: manual, schedule,│
      │ fixtures for external APIs         │ ──► │ (recorded) for  │ ──► │ event ─► job queue ─►      │
      │ simulated tool outages             │     │ consequential   │     │ runtime (policy, budgets,  │
      │ tool checks + grader               │     │ agents; deploy; │     │ approvals queue, audit)    │
      │ regression vs. last released version│    │ rollback        │     │ monitoring ─► improvement  │
      └────────────────────────────────────┘     └─────────────────┘     └────────────────────────────┘

   Capability library (shared by all agents)                 Data plane (per customer)
   knowledge search · read document · query data (read-only    datasets (SQL, read-only to agents)
   SQL) · search/create/update records · HTTP API endpoints     documents / knowledge
   (declarative connectors) · draft/send message · memory       records, memory, outbox
   save/recall · schedule task · calculate · escalate           secrets: env/secret manager only
```

### Components and responsibilities

| Component | Responsibility | Build or buy |
|---|---|---|
| **Spec** (`spec.py`) | Declarative agent definition: capabilities, instructions, workflow, tools and their config, data sources, triggers, memory policy, limits, success criteria, restrictions, tests, assumptions, open questions. Versioned in the database. | Custom (core IP). |
| **Capability library** (`components.py`) | Tested building blocks. Each declares its **risk class** (read, write_internal, write_external, external_comm, financial), config, and input schema, and can tailor its tool description to the agent (e.g. query_data shows the actual table schemas). | Custom framework; connectors reuse vendor APIs/MCP servers. |
| **Declarative HTTP connector** | One component that turns "method + URL + parameter schema + auth secret name" into a tool. Most business systems with a REST API (CRM, ERP, ticketing, Fortnox, Visma) can be connected without new code. | Custom, small. OAuth flows later via a provider (Nango) instead of building each one. |
| **Data plane** (`store.py`) | Platform DB (specs, runs, audit, jobs, approvals) plus a **separate data file per customer** for imported datasets. Agents get read-only SQL through an authorizer that only permits SELECT on that agent's allowed tables. | SQLite now → PostgreSQL with row-level security (Supabase/Neon/RDS in the EU) in production. |
| **Requirements pipeline** (`pipeline.py`) | Intake → discovery questions → analysis (opportunities, what to keep human, integrations and their status, risks, complexity, run volume) → design. Every stage is stored and editable. | Custom, on the Claude API with structured outputs. |
| **Validator/assembler** (`designer.py`) | Enforces policy on whatever the model produced: known components only, risk-based approval rules, unique tool names, memory tools only if memory is enabled, test references resolve, safety and failure test coverage present. | Custom. |
| **Runtime** (`runtime.py`) | Generic tool loop. Enforces per-agent turn and token budgets, approval policy (synchronous, or **queued** for unattended runs), sandbox fixtures and simulated failures, audit log. | Custom on the Claude API. Anthropic's Managed Agents is an alternative for agents that need a code sandbox (beta; data then lives in Anthropic's environment). |
| **Evaluation** (`evaluate.py`) | Categorised test cases, fixtures, failure injection, deterministic tool checks + grader model, carried-forward regression cases, cost per run, release gate. | Custom. |
| **Release** | Pass gate + recorded human approval for consequential agents + deploy/rollback. | Custom (in the store). |
| **Triggers and jobs** (`worker.py`) | Schedules and events become jobs in the DB; a worker runs due jobs for deployed agents, with retries; approvals needed by unattended runs go to a queue for a person. | Custom DB queue now. Move to a durable workflow engine (Temporal) or a Postgres queue library when volume requires. |
| **Monitoring** (`monitor.py`) | Per-agent and per-version: runs, completion, escalations, tool errors, approvals requested/denied, tokens, cost. | Custom tables now; export to OpenTelemetry/Langfuse later. |
| **Interfaces** | CLI now. Next: an HTTP API (FastAPI) used by a business dashboard and by inbound webhooks. | Custom. |

### Tools, APIs and infrastructure needed

| Need | Recommendation | Why |
|---|---|---|
| Models | Claude API; Opus-class for design/analysis/grading, per-agent model and effort set in the spec (cheaper models where tests show quality holds). | Strong tool use and structured outputs; one provider keeps the evaluation consistent. For customers who need processing in a specific region, Claude is also offered through AWS Bedrock and Google Vertex AI in EU regions. Check current regional availability and sign the matching data-processing terms before promising it. |
| Database | PostgreSQL (Supabase, Neon or RDS) in an EU region, row-level security by customer; pgvector for semantic search. | Isolation enforced by the database, not only by application code. |
| Secrets | A secret manager (AWS Secrets Manager, Doppler, Vault). Specs only ever hold the *name* of a secret. | Credentials never enter specs, logs, prompts or the model. |
| Integrations | 1) Vendor MCP servers where they exist. 2) Declarative HTTP endpoints. 3) Unified APIs (e.g. Merge for CRM/HRIS/accounting) and an OAuth broker (e.g. Nango) once many customers need the same category. 4) Hand-written connectors for high-value Swedish systems (Fortnox, Visma) when demand is proven. | Covers most systems without one-off code per customer. |
| Documents | Claude's native PDF input for extraction; an embeddings model (e.g. Voyage) + pgvector for retrieval when keyword search stops being enough. | Retrieval quality matters for knowledge agents. |
| Jobs | DB-backed queue now; Temporal or a Postgres queue later. | Durable scheduled/event work with retries. |
| Observability | Platform tables now; OpenTelemetry → Langfuse (self-hostable) later. | Traces for debugging, cost tracking per customer. |
| Hosting | Containers in an EU region (AWS eu-north-1, Fly.io, Hetzner). | Simple, close to Swedish customers. |

## 3. Requirements-to-agent pipeline

| Stage | Input → output | Stored as | Human role |
|---|---|---|---|
| Intake | Company description, process, problem, systems, goals | `projects.intake` | Customer/consultant writes it |
| Discovery | Intake → 6–12 targeted questions, each with why it matters | `projects.questions_json` | Customer answers (file or interactive) |
| Analysis | Intake + answers → process summary, automation opportunities (which steps automate, which stay human, value, risk), recommended agent and capabilities, integrations with status (supported / via HTTP API / missing), data needed, risks, complexity, expected runs per month, open questions | `projects.analysis_json` | Consultant reviews; can stop here |
| Design | All of the above → agent spec with workflow, tools, data sources, triggers, memory policy, limits, tests (task, quality, tool use, missing information, permission, unauthorised action, API failure, edge case) | `agent_versions` | Reviews/edits YAML |
| Validate | Applies risk policy, removes unsupported parts, records what's missing | same | – |
| Test | Sandbox, fixtures, failure injection, regression, cost | `test_runs` | Reads report |
| Approve & deploy | Required for consequential agents | `approvals`, `agents.deployed_version` | Named approver |
| Operate | Triggers → jobs → runs; queued approvals | `jobs`, `runs`, `pending_actions`, `audit_log` | Approves queued actions |
| Monitor & improve | Metrics, failures → spec changes → new version → regression suite | `runs`, `test_runs` | Decides changes |

The pipeline never claims a capability that doesn't exist. If an integration isn't supported, the analysis says so, the spec marks the data source as `needs_connector`, and it appears in open questions.

## 4. Testing, deployment, monitoring, security, maintenance

- **Testing:** every spec must include permission/unauthorised-action cases and at least one API-failure case when the agent uses external tools. All safety cases must pass, overall pass rate ≥ 90%, and **no regressions** against the last passed version. The previous version's cases are carried forward automatically. External APIs are never called in tests: fixtures supply responses; a missing fixture returns an error so the agent's error handling is exercised. Token cost per run is reported against the spec's budget.
- **Deployment:** an agent with any tool above read risk needs a recorded approval (who, when, note) for that exact version. Rollback = deploy an older passed version.
- **Runtime safety:** tools limited to the spec; financial and external-communication tools always need approval (the designer can't turn that off); data access is read-only SQL with an allowlist of tables; per-run turn and token budgets; content from emails, documents and APIs is treated as data; every call is audited.
- **Isolation:** every row is keyed by agent/customer; each customer's datasets are in their own file; secrets come from the environment/secret manager by name only. Production moves isolation into Postgres row-level security.
- **Monitoring:** completion rate, escalation rate, tool error rate, approvals requested/denied, tokens and cost per run, by version.
- **Maintenance:** spec changes create a new version, which must pass the regression suite before deployment.

What is **not** guaranteed today: data residency (it depends on deployment and provider agreements), role-based access for platform users (planned), and delivery of messages (the outbox records them; delivery connectors are not built yet).

## 5. Roadmap

**Stage 1: minimum viable foundation (this change)**
1. Spec v2 (workflow, data sources, triggers, memory, limits, risk-aware tools, categorised tests, assumptions), with automatic upgrade of v1 specs.
2. General capability library with risk classes: knowledge search, read document, read-only data queries over imported datasets, record search/create/update, declarative HTTP endpoints, draft/send message, memory save/recall with retention, schedule task, calculate, escalate.
3. Requirements pipeline: intake → discovery → analysis → design, persisted per project.
4. Evaluation: categories, fixtures, failure injection, regression carry-over, safety gate, cost per run.
5. Release: recorded approvals for consequential agents.
6. Operation: triggers (schedule/event), job queue + worker, queued approvals for unattended runs, per-run budgets.
7. Monitoring command with cost.
8. Examples beyond email: request routing with company data; a scheduled BI report.

**Stage 2: product for pilot customers**
- HTTP API (FastAPI) + business dashboard (projects, specs, test reports, approvals queue, monitoring) replacing the demo page.
- Postgres with row-level security; platform users and roles (owner, operator, viewer) per customer.
- Secret manager integration; OAuth via a broker; first hand-written connectors chosen by pilot demand (likely Microsoft 365 mail/calendar, Fortnox, a CRM).
- Inbound webhooks for event triggers; outbound delivery for the outbox (email via Microsoft Graph, Slack, webhooks).
- Document ingestion (PDF/Word) and semantic retrieval.
- Improvement loop: from failed/escalated runs, propose spec changes and new test cases for human review.

**Stage 3: long-term**
- Generated custom components: the model writes a new connector or transform when the library lacks one, which runs in a sandbox, passes generated tests, and is reviewed by a human before entering the library.
- Deterministic workflow engine for multi-step processes that need guaranteed ordering (beyond model-followed workflow steps).
- Multi-agent compositions (an orchestrator delegating to specialised agents).
- Self-serve onboarding where the customer answers discovery questions directly.
- Per-customer deployment options (dedicated database/region) for regulated customers.

## 6. Implementation plan for stage 1

Changes fit the existing modules; nothing working is thrown away.

| Step | Files | Notes |
|---|---|---|
| Spec v2 + upgrade of v1 specs | `spec.py` | LLM-facing models keep every field required (structured outputs); a migration fills defaults for stored v1 specs. |
| Capability library | `components.py`, `datasets.py` | Component base class gets risk, per-agent tool definitions and a context with sandbox/fixtures. |
| Store additions | `store.py` | New tables: projects, datasets, memory, jobs, pending_actions, approvals; columns for record type/status and message channel; additive migration. |
| Runtime | `runtime.py` | Budgets, queued approvals, sandbox failure injection and fixtures. |
| Pipeline | `pipeline.py`, `designer.py` | Discovery, analysis, design; validator applies risk policy and coverage checks. |
| Evaluation | `evaluate.py` | Categories, fixtures, failures, regression carry-over, safety gate, cost. |
| Operation | `worker.py` | Triggers, jobs, queued actions. |
| Monitoring | `monitor.py` | Metrics and cost. |
| CLI | `cli.py` | New commands; existing ones kept working. |
| Examples and tests | `examples/`, `tests/` | Offline tests for every new capability. |

## 7. Status after the stage 1 change

Implemented and covered by offline tests (`pytest`, 24 tests):

- Spec v2 with automatic upgrade of v1 specs (`spec.py`).
- Capability library of 14 components with risk classes, per-agent tool definitions and config validation, including the declarative HTTP connector and read-only SQL over per-customer datasets (`components.py`, `datasets.py`).
- Validator enforcing approval policy, memory policy, limits, triggers and test coverage (`designer.py`).
- Pipeline: discovery questions, answers, analysis with integration status and cost estimate, design; persisted per project (`pipeline.py`).
- Runtime: turn and token budgets, queued approvals for unattended runs, simulated outages and fixtures in tests, errors returned to the agent (`runtime.py`).
- Evaluation: categories, regression carry-over, safety gate, cost per run (`evaluate.py`).
- Release: recorded approvals for consequential agents (`store.py`, `cli.py`).
- Triggers and jobs: schedules, events, agent follow-ups, retries and escalation (`worker.py`).
- Monitoring with cost (`monitor.py`).
- Examples: operations request router (data + records + contractor API) and BI agent (scheduled report + memory).

Not yet verified against the live API: model behaviour, structured-output acceptance of the larger schemas, and real HTTP connectors. The first live run should be `agentgen test` on both examples.

Next priorities (stage 2, in order): run both examples live and fix what fails; HTTP API + dashboard (projects, test reports, approvals queue, monitoring); Postgres with row-level security and user roles; the first delivery and inbound connectors chosen by pilot demand; the improvement loop from failed runs to proposed spec changes.

## 8. Status after the web application change

The engine now has a web application (`agentgen/web/`, started with `agentgen serve`):

- **API** (FastAPI): accounts and sessions (scrypt passwords, HttpOnly SameSite=Strict cookies, CSRF header check, login throttling, security headers incl. CSP); every endpoint scoped to the user's organisation; administrators approve, deploy and change settings.
- **Background operations** for model work (questions, analysis, design, tests, runs) that the browser polls; interrupted operations are marked failed on restart.
- **Metering and spending limit**: every model call goes through a client that refuses calls once the organisation's monthly budget is spent and records tokens and cost by purpose.
- **Local deployment**: a worker thread runs deployed agents' schedules, events and follow-ups; unattended actions that need approval are queued for a person.
- **Sandbox runs**: act on a copy of the data with external APIs simulated, but are recorded in the real execution history.
- **Security fixes found while building**: organisation-scoped secrets (a spec can no longer name the server's own credentials), SSRF blocking for internal addresses, random suffixes for agent names that collide across organisations.
- **UI** (dependency-free JavaScript): overview, projects with the six-step requirements flow, agent overview/configuration/tests/run/deployment/activity, testing, deployed agents, execution history with run details, approvals, monitoring, data & integrations, settings.

Verified offline: 33 pytest tests (engine + HTTP API, including isolation, approval enforcement, budget, failures) and a 13-step browser test of the full journey. Not verified: behaviour with the real model (no API key was available).

Highest remaining production risks: unverified real-model quality and cost; SQLite and in-process worker (single server, no HA); application-level tenancy only; no delivery or inbound connectors; no SSO, password reset or email verification; secrets via environment variables; no backups or data-retention tooling.

## 9. Status after preparing for the real Anthropic API

The model is now behind a replaceable provider interface, and the lifecycle is complete through
diagnosis, fixing and restoring. Everything was verified offline; **no real API call has been
made**.

- **Provider interface** (`agentgen/providers/`): `structured()` (schema-validated outputs for
  everything the engine parses), `turn()` (agent tool use) and `check()` (no-token connection
  test).
  - **Anthropic provider**: built on the official SDK. The key comes only from `ANTHROPIC_API_KEY`;
    requests are pinned to the official endpoint, streamed, and use structured outputs, prompt
    caching and server-side model fallbacks.
  - **Errors**: SDK errors become provider-neutral `ModelError`s, each with a message and a next
    step for the user (key, billing, rate limit, timeout, overload, bad request, refusal,
    malformed or cut-off output).
  - **Configuration**: environment variables for the default and grader models, timeout and retries.
- **Metering**: every provider is wrapped by a budget guard. It checks the month's spend plus
  this call's projected cost *before* calling, then records provider-reported tokens and the
  estimated cost.
- **Simulated vs real**: every project stage, version, test report, run and usage row records
  which provider produced it, and the scripted model is labelled *simulated* everywhere. Simulated
  usage has its own budget line. Simulated test results can't approve or deploy on a real-model
  server.
- **Lifecycle**:
  - discovery records missing information and contradictions, and passes them to analysis and design;
  - test cases can be added (by hand or suggested by the model), removed, and checked exactly (`must_include`);
  - failed runs and test cases get a deterministic diagnosis, and a model-proposed fix (with a
    regression test) becomes a new draft version that must pass again;
  - any version can be restored as a new draft, or rolled back to directly if it was approved.
- **Safety**:
  - **Inputs**: every tool input is validated against its schema before running.
  - **Retried jobs**: each job is an idempotency scope, so actions from earlier attempts are not
    repeated, and the job is told when a person already denied or failed an action.
  - **Approvals**: claimed atomically, so an action runs once even if approved twice.
  - **HTTP tool**: redirects are not followed, so a redirect can't reach internal addresses or
    receive credentials.
  - **Restarts**: interrupted jobs are retried after a restart.
- **Bugs found by the offline evaluation suite and fixed**:
  - discovery findings were dropped before analysis;
  - whitespace-only required fields were accepted;
  - reserved dataset names crashed the import;
  - the row limit counted the header;
  - extra CSV cells were silently dropped;
  - a too-wide CSV destroyed the existing dataset;
  - codes like `2026_01` became numbers;
  - retried jobs were told that decided actions were still waiting.
- **First real-model evaluation**: `agentgen live-eval --budget 5` checks the bundled sales agent
  against known figures, with a hard cap and a separate database (docs/DEPLOYMENT.md).

Next, in order:

1. Run `agentgen live-eval` with a key and fix what the real model gets wrong.
2. Move to Postgres with backups and row-level security.
3. Add account recovery and MFA/SSO.
4. Build the first delivery and inbound connectors chosen by a pilot customer.
