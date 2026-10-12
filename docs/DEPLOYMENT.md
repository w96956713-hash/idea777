# Deployment and the real model

This guide covers installing and running agentgen, connecting the real Anthropic model, the
first real-model evaluation, and what must change before real customers use it.

**Status (2026-10-12).** Everything has been built and tested offline with a scripted test
model and mocked HTTP. **No real Anthropic API call has been made yet**, so how well the real
model generates, runs and grades agents has not been measured. "First real-model evaluation"
below is how to do that safely.

## 1. What runs

One Python process (FastAPI + uvicorn) serves the web app and API, runs long operations (question
generation, design, tests, fixes) in a small thread pool, and runs the built-in background worker
(schedules, events, follow-ups). State is in SQLite:

| Path | Contents |
|---|---|
| `$AGENTGEN_DB` (default `agentgen.db`) | Organisations, users, sessions, projects, agent versions, runs, audit log, approvals, jobs, usage and costs |
| `$AGENTGEN_DB.data/customer_<id>.sqlite` | Each organisation's uploaded datasets (one file per organisation) |

Run **one** server process per database. The worker, operations and login throttling live in
that process, so it can't be scaled out yet (see "Production blockers").

## 2. Install

Requirements: Python 3.10+ (tested on 3.13) and pip. Node.js with Playwright is only needed for
the optional browser test.

```bash
git clone <repository> agentgen && cd agentgen
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'          # the app plus pytest and the HTTP mocking library used by tests
pytest                           # the whole offline suite: no API key, no network
agentgen serve                   # http://127.0.0.1:8000
```

`pip install .` (without `[dev]`) installs only what the server needs:
`anthropic>=1.13`, `pydantic>=2`, `pyyaml>=6`, `fastapi>=0.110`, `uvicorn>=0.29`.

Optional browser test of the whole journey, on a fresh server with the scripted model:

```bash
npm install -g playwright && npx playwright install chromium    # once
AGENTGEN_UI_TEST=1 pytest tests/test_ui.py
```

## 3. Configuration

All configuration is environment variables, read by the server only. Nothing secret is sent to
the browser, logged or stored in the database.

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | (unset) | The Claude API key. **The only place the key is read from.** Without it the app works, but anything that needs the model says so and stops before calling. |
| `AGENTGEN_DEFAULT_MODEL` | `claude-opus-5-5` | Model for the requirements pipeline and new agents. Each agent version also records its own model (editable in Configuration). |
| `AGENTGEN_GRADER_MODEL` | the default model | Model that grades test results. |
| `AGENTGEN_MODEL_TIMEOUT_SECONDS` | `600` | Per request. Requests are streamed. |
| `AGENTGEN_MODEL_MAX_RETRIES` | `2` (0–5) | SDK retries for rate limits, overloads and connection errors. |
| `AGENTGEN_DEFAULT_BUDGET_USD` | `25` | Monthly model budget for an organisation that hasn't set its own (Settings). |
| `AGENTGEN_DB` | `agentgen.db` | Database file (datasets go in `<file>.data/`). |
| `AGENTGEN_ALLOW_SIGNUP` | `1` | Set `0` to stop new organisations signing up. |
| `AGENTGEN_SECURE_COOKIES` | `0` | Set `1` when served over HTTPS (it must be, in production). |
| `AGENTGEN_ORG<id>_<NAME>` | (unset) | Secrets for agents' external APIs, per organisation. An agent spec names `CRM_TOKEN`; organisation 3's server reads `AGENTGEN_ORG3_CRM_TOKEN`. Settings shows each organisation its prefix. |

`ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN` are deliberately ignored: requests always go
to `https://api.anthropic.com` with the key above, so nothing can redirect the key elsewhere.

`agentgen serve` options: `--host` (default `127.0.0.1`), `--port` (8000), `--no-worker`,
`--poll` (worker interval, seconds), `--quiet`.

## 4. Adding the Anthropic API key later

1. Create a key in the Anthropic Console (Settings → API keys). Use a dedicated workspace for
   this platform and set a **spend limit** there as well. That cap is independent of agentgen's
   own budgets.
2. Put the key in the server's environment, never in the repository or a file the app serves:
   - systemd: `Environment=` in a drop-in, or `EnvironmentFile=/etc/agentgen/env`
     (owner root, mode `600`), e.g. `ANTHROPIC_API_KEY=sk-ant-...`;
   - a container platform: its secret store (Fly secrets, Render/Railway env vars, Kubernetes
     Secret, AWS Secrets Manager or GCP Secret Manager injected as env);
   - locally: `export ANTHROPIC_API_KEY=...` in the shell that runs `agentgen serve`.
3. Restart the server.
4. Sign in as an administrator → **Settings → AI model provider**. "API key" should read
   *configured on the server*. Press **Check connection**. This uses the Models API, which costs no
   tokens, to confirm that the key works and the default model is available. A wrong key, missing
   permission or unknown model is reported with what to do.
5. Run the first real-model evaluation (next section) before trusting anything else.
6. Test results recorded in test mode are labelled *simulated*. On a server with the real model,
   such results **cannot be used to approve or deploy**: re-run each agent's test suite first.

To rotate the key: create a new one, replace the variable, restart, and press Check connection.
Then revoke the old key in the Console.

## 5. First real-model evaluation

A small, capped, repeatable check of the real model on a known problem, in a throwaway database:

```bash
export ANTHROPIC_API_KEY=...
agentgen live-eval --budget 5            # optional: --model claude-sonnet-5-5 --dir ./live-eval-1
```

What it does:

1. Refuses to start without a key. It then checks the key and model through the Models API (no tokens).
2. Creates a separate database (a new temp folder, or `--dir`), so it never touches application
   data. It loads the bundled weekly-sales example: 12 orders whose correct figures are known.
3. Runs that agent's 7 test cases with the real model: a weekly report, revenue by region, a
   question that can't be answered (margins), a request to email an investor, a request to break
   privacy rules, a data outage, and an empty period. Each case is checked three ways:
   - exact assertions: the tools used, the tools forbidden, and the stated figures for revenue by region
     (10 325, 3 294, 2 297 and 4 735 SEK);
   - a normal stop;
   - the grader model.
4. Every call is metered. When the next call would exceed `--budget` (default 5 USD), the run
   stops and says so. Expect roughly 1–3 USD with the default model; the real figure is printed
   at the end. It is an estimate at list prices from provider-reported tokens.
5. It prints each case as it runs and writes `live_eval_report.json` with every transcript.

The exit code is 0 only if the suite passed. Then, in the web app with the key set:

- Create one project through the whole journey with a real business description (≈ 3–4 model
  calls), read the questions, analysis and generated spec critically, and run its tests.
- Do one sandbox run, read the run's steps and diagnosis, and compare the cost in Monitoring
  with the Anthropic Console's usage page.

Record what you find (which cases failed and why, cost per run, latency). That is the first
real evidence of quality. Nothing produced in test mode is.

## 6. Database

- SQLite in WAL mode. Keep both the database file and the `.data/` folder on **persistent
  storage** (a mounted volume). A container's own filesystem is wiped on redeploy.
- Back up while running with SQLite's online backup, not by copying files:
  `sqlite3 agentgen.db ".backup '/backups/agentgen-$(date +%F).db'"`, plus each
  `agentgen.db.data/customer_*.sqlite` the same way. Copy the backups off the machine,
  keep several generations, and **test a restore**.
- Schema changes are applied automatically at start-up (new tables and columns only).
- The SQL is written to be portable to Postgres, but no Postgres driver is wired in yet.

## 7. Hosting

Minimum for anything beyond a private trial:

- Run behind an HTTPS reverse proxy (Caddy, nginx or the platform's load balancer), with
  `agentgen serve --host 127.0.0.1` and `AGENTGEN_SECURE_COOKIES=1`.
- Create your organisation first, then set `AGENTGEN_ALLOW_SIGNUP=0` and add people from
  Settings → Team.
- Exactly one instance per database (the worker and operations are in-process).
- Outbound network: `api.anthropic.com:443`, plus whatever external APIs your agents are
  configured to call. Agents can't reach private or internal addresses, and redirects are not
  followed. For defence in depth, also restrict egress at the network level (see blockers).
- Restarts are safe: interrupted operations are marked failed (people can retry), and interrupted
  background jobs are retried without repeating actions they already performed.

Example systemd unit:

```ini
[Unit]
Description=agentgen
After=network-online.target

[Service]
User=agentgen
WorkingDirectory=/srv/agentgen
EnvironmentFile=/etc/agentgen/env
Environment=AGENTGEN_DB=/var/lib/agentgen/agentgen.db AGENTGEN_SECURE_COOKIES=1 AGENTGEN_ALLOW_SIGNUP=0
ExecStart=/srv/agentgen/.venv/bin/agentgen serve --host 127.0.0.1 --port 8000 --quiet
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## 8. Security model (what is enforced today)

- **Organisations** are isolated in the API layer. Every query is scoped to the signed-in user's
  organisation, and another organisation's objects return 404. Datasets are stored in a separate
  file per organisation.
- **Permissions are enforced by the backend**, never by the model:
  - an agent sees only its own tools;
  - every tool input is validated against its schema before anything runs;
  - SQL is read-only and limited to the tables the agent is allowed (enforced by SQLite's authorizer).
- **Approvals.** External writes, external messages and anything financial always need a
  person's approval; this cannot be switched off. Only administrators approve. Approving twice, or
  by two people at once, runs an action once. Retried background jobs never repeat an action that
  already happened, and are told when a person denied it.
- **Release.** Only a version whose latest test run passed, with every safety and permission
  case passing, can be approved. Agents with consequential tools also need a recorded approval
  before deployment. Results from the scripted test model can't release anything on a
  real-model server.
- **Budgets** are checked before every model call, against a projection of that call's cost.
  Simulated usage never counts against the real budget.
- **Secrets.** The model key is read only from `ANTHROPIC_API_KEY`. Agent secrets are read only
  from `AGENTGEN_ORG<id>_*`, so an agent can't reach another organisation's secrets or the
  platform's. Secrets are never shown, logged or stored.
- **Web.** Sessions are HttpOnly, SameSite=Strict cookies. Passwords are hashed with scrypt.
  State-changing requests need a CSRF header, a strict Content-Security-Policy is set, and sign-in
  is throttled. Error messages never include stack traces.

## 9. Production blockers

These must be addressed before real customer data or real external actions:

1. **Backups and restore**: automated, off-machine, monitored, with a tested restore and a
   stated recovery point and time.
2. **Persistent, managed database**: move to Postgres (managed, with point-in-time recovery).
   SQLite on one disk is a single point of failure.
3. **Account recovery and stronger sign-in**: there is no password reset, email verification,
   MFA or SSO. A locked-out administrator currently needs a database operation.
4. **Stronger tenant isolation**: isolation is enforced in application code. Add database-level
   row security (Postgres RLS) or a database per organisation, and encrypt datasets per
   organisation.
5. **Secret management**: environment variables are acceptable for one key. Organisation secrets
   should move to a secret manager, with rotation and an audit of access.
6. **Network egress control**: the internal-address check resolves the host before connecting,
   so DNS rebinding remains possible. Enforce egress at the network level (an egress proxy or
   firewall allow-list).
7. **Scale-out and durability**: operations and the worker run in-process. A queue (e.g.
   Postgres-backed jobs) is needed for more than one instance. Login throttling is per process.
8. **Observability**: structured logs, error alerting, uptime checks, and a usage dashboard
   reconciled against the Anthropic invoice.
9. **Legal and data**: a data processing agreement, data residency, retention and deletion on
   request, and a privacy notice. None of these is implied by this code.
10. **Real-model validation**: section 5, then real projects with a pilot customer.

Not built yet (by design at this stage): message delivery connectors (messages go to an
outbox), inbound webhooks and email listeners, payments, managed hosting.
