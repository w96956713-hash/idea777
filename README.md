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
- **Web dashboard and HTTP API.** The CLI is the interface today. The earlier browser demo predates
  this architecture and is email-centric; don't use it to judge the product.
- **Platform user roles**, Postgres with row-level security, a secret manager, OAuth, and semantic
  document search. Planned for stage 2 (see the architecture doc).
- **Data residency.** It depends on where you deploy and which model provider agreement applies; no
  guarantee is implied by this code.
- **Real-API verification.** The offline tests use a scripted model. Run `agentgen test` with an API
  key to measure real behaviour.
