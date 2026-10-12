# agentgen — AI agent generator

Turns a customer's requirements into a tested, versioned AI agent, and stores
everything (customers, requirements, agent versions, test results, runs and an audit
log) in a database.

```
meeting notes ──► design ──► spec (draft) ──► human review ──► test ──► deploy ──► run
                   (Claude)   stored in DB      export/import    sandbox   only if      approvals,
                                                                 + judge   tests pass   audit log
```

## How it works

| Step | What happens | Code |
|---|---|---|
| 1. Requirements | Meeting notes are saved against the customer. | `store.py` |
| 2. Design | Claude reads the notes and writes a structured **spec**: instructions, which tools to use, which actions need human approval, 8–12 test cases, and open questions. | `designer.py`, `spec.py` |
| 3. Assemble | The agent is built only from **reviewed components** (knowledge search, email draft/send, create record, escalate to human). Generated agents never contain generated code. | `components.py` |
| 4. Test | Each test case runs in a **sandbox copy** of the agent's data. A case passes only if the right tools were used *and* a grader model agrees the behaviour matches. | `evaluate.py` |
| 5. Deploy | Only versions that passed can be deployed. Deploying an older passed version is a rollback. | `store.py` |
| 6. Run | Sensitive actions stop for human approval. Every step, tool call and token count is written to the database. | `runtime.py` |

## Quick start

```bash
pip install -e '.[dev]'
export ANTHROPIC_API_KEY=...        # from console.anthropic.com

# Try the example (an accounting firm's email inbox)
agentgen import examples/accounting_firm/spec.yaml --customer "Andersson Redovisning AB"
agentgen knowledge client-email-triage examples/accounting_firm/knowledge/*.md
agentgen test client-email-triage -v
agentgen deploy client-email-triage
agentgen chat client-email-triage

# Or generate a new agent from your own meeting notes
agentgen design examples/accounting_firm/requirements.md --customer "Andersson Redovisning AB"
agentgen export client-email-triage -o review.yaml   # read it, fix it
agentgen import review.yaml --customer "Andersson Redovisning AB"  # saved as next version
```

Other commands: `agentgen list`, `agentgen components`, `agentgen usage <agent>`.
Run the offline tests with `pytest` (no API key needed).

## The database

SQLite by default (`agentgen.db`, or set `AGENTGEN_DB`). Tables:

- `customers`, `requirements`: who the work is for and what they asked for
- `agents`, `agent_versions`: every spec version, its status (draft/passed/failed) and which version is live
- `test_runs`: full test reports per version
- `knowledge_docs`: each agent's company documents
- `runs`, `audit_log`: every conversation, tool call, approval decision and token count
- `records`, `outbox`, `escalations`: what the agents produced (cases, emails, hand-offs)

Every customer-owned row is keyed by agent (and through it, customer), and the runtime only
ever reads and writes rows for the agent it is running.

The SQL is portable. To move to hosted PostgreSQL for production, replace `connect()` in
`store.py` (and the `?` placeholders become `%s` with psycopg).

## Adding an integration

Add one `Component` subclass to `components.py` (name, description, input schema, `run`).
The designer sees it in the catalog automatically. Next likely components: Microsoft 365 mail
(read inbox, deliver approved `outbox` rows), Fortnox, Visma.

## Models

The designer, the agents and the test judge use `claude-opus-5-5` with server-side refusal
fallback enabled. Each agent's model and effort level live in its spec, so a cheaper model can be
set per agent once tests show the quality holds.
