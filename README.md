# agentgen: bespoke AI agents from business requirements

A company explains what it needs. agentgen works out what is worth automating, designs a
specialised agent, assembles it from tested capabilities, tests it against realistic and
adversarial scenarios, and, after a person approves it, deploys and monitors it.

Agents are **data, not code**: each agent is a versioned spec executed by one generic
runtime with a shared capability library. Sales, service, research, BI, operations, document,
finance and email agents are different combinations of the same components, not separate
implementations.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the audit, architecture and roadmap.

```
intake ─► discovery questions ─► analysis ─► design ─► validate ─► test ─► approve ─► deploy ─► operate ─► monitor
                                  (what to     (spec)    (policy)   (sandbox,  (named     (pass     (triggers,  (cost,
                                  automate,                          failures,  person)    gate)     jobs,       errors,
                                  integrations,                      regression)                     approvals)  escalations)
                                  cost)
```

## Run the web application

```bash
pip install -e '.[dev]'
export ANTHROPIC_API_KEY=...          # needed to generate, test and run agents (server-side only)
agentgen serve                         # http://127.0.0.1:8000
```

Create an account (this creates your organisation), then:

1. **Data & integrations**: upload CSV exports (e.g. `examples/sales_insights/data/orders.csv` and
   `products.csv`) and any policy documents.
2. **Create an agent**: describe the company, the problem, the current workflow, the desired outcome,
   systems and constraints.
3. On the project page: **Generate questions** → answer → **Analyse requirements** → add corrections →
   **Generate agent**. The designer only connects the agent to data you actually uploaded.
4. On the agent: review **Overview** (responsibilities, workflow, tools, permissions, data, limits, tests),
   adjust **Configuration** (each save is a new version), **Run test suite**, **Run a task** in the sandbox.
5. **Versions & deployment**: approve a version whose tests passed (safety cases included) and deploy it.
   Then run live tasks, send events, and watch **Execution history**, **Approvals** and **Monitoring**.

Settings has the monthly model budget (default $25); every model call is metered against it.
`--no-worker` disables the background worker; `AGENTGEN_ALLOW_SIGNUP=0` disables sign-up;
`AGENTGEN_SECURE_COOKIES=1` when served over HTTPS.

**Real vs simulated.** Agent runs, tools, SQL over your data, records, drafts, memory, tests, approvals,
the worker and all metrics are real. In sandbox runs and tests, external APIs are simulated (fixtures or
a clear "not called" result) and actions only touch a copy of the data. Messages are written to the outbox;
no delivery connector exists, so nothing is actually sent. Live external API calls need the organisation's
secret set on the server (`AGENTGEN_ORG<id>_<NAME>`, shown in Settings).

## Tests

```bash
pytest                                         # 33 tests, offline, scripted model
AGENTGEN_UI_TEST=1 pytest tests/test_ui.py     # browser test of the full journey (Node.js + Playwright)
```

The scripted model (`tests/fake_model.py`) stands in for Claude in tests only. Everything else — the
database, pipeline, validation, runtime, tools, evaluation, approvals, worker and UI — is the real code.

## What an agent can be built from

| Capability | Component(s) | Risk class |
|---|---|---|
| Company documents | `knowledge_search`, `read_document` | read |
| Company data (SQL, read-only, allowlisted tables) | `query_data` over imported datasets | read |
| Business records (cases, leads, flags…) | `search_records`, `create_record`, `update_record` | read / write_internal |
| Any REST API (CRM, ERP, ticketing, contractors…) | `http_request`: one configured endpoint = one tool | read / write_external / financial |
| Communication | `draft_message`, `send_message` (email, Slack, Teams, SMS, webhook) | write_internal / external_comm |
| Memory with retention | `remember`, `recall` | write_internal / read |
| Planning and follow-up | `schedule_task` | write_internal |
| Exact arithmetic | `calculate` | read |
| Hand-off | `escalate_to_human` (always present) | write_internal |

Policy is enforced by the platform, not left to the model: external writes, external
communication and money always need a person's approval; agents only see their own tools and
data; each run has turn and token budgets; every step is audited.

## Quick start

```bash
pip install -e '.[dev]'
pytest                                   # offline tests, no API key needed
export ANTHROPIC_API_KEY=...
```

**From requirements (the main flow):**

```bash
agentgen project new examples/property_requests/intake.md --customer "Hemvist Fastigheter AB" --title "Tenant requests"
agentgen project answer 1        # answer the discovery questions
agentgen project analyze 1       # opportunities, what stays human, integrations, risks, cost estimate
agentgen project design 1        # agent spec v1, validated against platform policy
```

**Or load a finished example and run it:**

```bash
# Operations: route tenant requests using company data and a contractor API
agentgen data import "Hemvist Fastigheter AB" tenants examples/property_requests/data/tenants.csv
agentgen data import "Hemvist Fastigheter AB" properties examples/property_requests/data/properties.csv
agentgen import examples/property_requests/spec.yaml --customer "Hemvist Fastigheter AB"
agentgen knowledge service-request-router examples/property_requests/knowledge/routing_policy.md
agentgen tools service-request-router    # tools, risk classes, approval policy
agentgen test service-request-router     # sandbox, fixtures, simulated outages, grader
agentgen approve service-request-router --by "Your Name"
agentgen deploy service-request-router
agentgen trigger service-request-router new_request --payload "Omar Haddad, 1203: no heating" --run-now
agentgen actions list                    # approvals waiting from unattended runs
agentgen monitor service-request-router

# Business intelligence: answers questions and drafts a weekly report on a schedule
agentgen data import "Nordhem Design" orders examples/sales_insights/data/orders.csv
agentgen data import "Nordhem Design" products examples/sales_insights/data/products.csv
agentgen import examples/sales_insights/spec.yaml --customer "Nordhem Design"
agentgen test weekly-sales-insights && agentgen deploy weekly-sales-insights
agentgen worker --once                   # runs due schedules, events and follow-ups
```

`examples/accounting_firm` is a version-1 email agent, kept to show that older specs are upgraded
automatically.

## Testing and release

- Test cases have categories: task, quality, tool_use, missing_info, permission,
  unauthorized_action, api_failure, edge_case.
- Tests run on a sandbox copy of the agent's data. External APIs are never called: `fixtures`
  supply responses, `fail_tools` simulates outages.
- A case passes when required tools were used, forbidden tools weren't attempted, the run ended
  normally, and a grader model agrees with the expected behaviour.
- A version passes when ≥90% of cases pass, **every** safety case passes, and nothing that passed in
  the last passed version now fails (its cases are carried forward automatically).
- Agents that can change records, act on external systems, communicate externally or move money also
  need a recorded approval (`agentgen approve`) before `deploy`. Rollback = deploy an older passed version.

## What is not built yet

These are stated honestly so nothing is mistaken for a working feature:

- **Message delivery.** `send_message` queues messages in the outbox (`pending_delivery`); channel
  connectors (Microsoft Graph, Slack, SMS) are not built.
- **Inbound connectors.** Webhooks/inbox listeners that turn real emails or portal submissions into
  events (`agentgen trigger` is the entry point they will call).
- **Production hosting.** "Deployed" means running on the server you started with `agentgen serve`
  (local deployment with a built-in worker). There is no managed hosting, scaling or high availability.
- **Production-grade tenancy and auth.** Organisations are isolated in application code (every query is
  scoped) and datasets are in per-organisation files; there is no Postgres row-level security, SSO,
  email verification, password reset or audit export. Postgres, a secret manager, OAuth and semantic
  document search are planned (see the architecture doc).
- The earlier browser demo artifact predates this architecture and is email-centric; it is not the product.
- **Data residency.** It depends on where you deploy and which model provider agreement applies; no
  guarantee is implied by this code.
- **Real-model verification.** No API key was available while building this, so no real Claude call has
  been made. The offline tests use a scripted model; run the journey with a key to measure real quality.
