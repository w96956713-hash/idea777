"""Database layer. Everything the platform knows lives here.

Two kinds of storage:
- the platform database (this file's SCHEMA): customers, projects, agent versions, tests,
  runs, audit log, jobs, approvals and what agents produced;
- one data file per customer (`<data_dir>/customer_<id>.sqlite`) holding datasets the
  customer imported. Agents get read-only SQL access to it (see datasets.py).

SQLite by default; the SQL is portable to PostgreSQL. Every customer-owned row is keyed by
agent (and through it customer) and every query used by agents filters on it.
"""

import json
import secrets
import shutil
import sqlite3
import tempfile
import weakref
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .spec import AgentSpec

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requirements (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    title TEXT NOT NULL,
    intake TEXT NOT NULL,
    questions_json TEXT,
    answers_json TEXT,
    analysis_json TEXT,
    agent_name TEXT,
    stage TEXT NOT NULL,  -- intake | discovery | analysed | designed
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    name TEXT NOT NULL UNIQUE,
    deployed_version INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_versions (
    id INTEGER PRIMARY KEY,
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    version INTEGER NOT NULL,
    requirements_id INTEGER REFERENCES requirements(id),
    spec_json TEXT NOT NULL,
    status TEXT NOT NULL,  -- draft | passed | failed
    created_at TEXT NOT NULL,
    UNIQUE (agent_id, version)
);
CREATE TABLE IF NOT EXISTS test_runs (
    id INTEGER PRIMARY KEY,
    agent_version_id INTEGER NOT NULL REFERENCES agent_versions(id),
    pass_rate REAL NOT NULL,
    passed INTEGER NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY,
    agent_version_id INTEGER NOT NULL REFERENCES agent_versions(id),
    approved_by TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_docs (
    id INTEGER PRIMARY KEY,
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS datasets (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    name TEXT NOT NULL,
    columns_json TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (customer_id, name)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    version INTEGER NOT NULL,
    input TEXT NOT NULL,
    output TEXT,
    stop_reason TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    run_id INTEGER REFERENCES runs(id),
    event TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY,
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    table_name TEXT NOT NULL,  -- the record type, e.g. case, lead, invoice_flag
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY,
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    status TEXT NOT NULL,  -- draft | pending_delivery
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS escalations (
    id INTEGER PRIMARY KEY,
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    reason TEXT NOT NULL,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory (
    id INTEGER PRIMARY KEY,
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    key TEXT NOT NULL,
    content TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (agent_id, key)
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    due_at TEXT NOT NULL,
    input TEXT NOT NULL,
    source TEXT NOT NULL,  -- schedule | event:<name> | agent | manual
    status TEXT NOT NULL,  -- pending | running | done | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    run_id INTEGER REFERENCES runs(id),
    error TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,  -- admin | member
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS org_settings (
    customer_id INTEGER PRIMARY KEY REFERENCES customers(id),
    monthly_budget_usd REAL NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    kind TEXT NOT NULL,
    target_json TEXT NOT NULL,
    status TEXT NOT NULL,  -- queued | running | succeeded | failed
    result_json TEXT,
    error TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_usage (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    purpose TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    usd REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tool_executions (
    id INTEGER PRIMARY KEY,
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    idem_key TEXT NOT NULL,
    tool TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (agent_id, idem_key)
);
CREATE TABLE IF NOT EXISTS pending_actions (
    id INTEGER PRIMARY KEY,
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    run_id INTEGER REFERENCES runs(id),
    tool TEXT NOT NULL,
    input_json TEXT NOT NULL,
    status TEXT NOT NULL,  -- pending | approved | denied
    decided_by TEXT,
    result TEXT,
    created_at TEXT NOT NULL
);
"""

# Columns added after the first release; created on older databases.
ADDED_COLUMNS = [
    ("outbox", "channel", "TEXT NOT NULL DEFAULT 'email'"),
    ("records", "status", "TEXT NOT NULL DEFAULT 'open'"),
    ("records", "updated_at", "TEXT"),
    ("runs", "trigger_source", "TEXT NOT NULL DEFAULT 'manual'"),
    ("runs", "duration_ms", "INTEGER"),
    ("runs", "error", "TEXT"),
    ("projects", "intake_json", "TEXT"),
    ("projects", "amendments", "TEXT"),
    ("projects", "questions_provider", "TEXT"),
    ("projects", "analysis_provider", "TEXT"),
    ("projects", "design_provider", "TEXT"),
    ("agent_versions", "origin", "TEXT NOT NULL DEFAULT 'generated'"),
    ("agent_versions", "provider", "TEXT"),
    ("agent_versions", "note", "TEXT"),
    ("runs", "provider", "TEXT"),
    ("runs", "simulated", "INTEGER NOT NULL DEFAULT 0"),
    ("model_usage", "provider", "TEXT NOT NULL DEFAULT 'anthropic'"),
    ("model_usage", "simulated", "INTEGER NOT NULL DEFAULT 0"),
    ("pending_actions", "idem_key", "TEXT"),
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _now() -> str:
    return utcnow().isoformat()


def connect(path: str) -> sqlite3.Connection:
    # Each Store (connection) is used by one request or task at a time, but FastAPI may open and
    # close it on different worker threads, so the same-thread check is relaxed.
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")  # readers don't block the background worker
    return conn


class Store:
    def __init__(self, path: str = "agentgen.db", data_dir: str | None = None, migrate: bool = True):
        self.path = path
        self.conn = connect(path)
        if migrate or path == ":memory:":
            self.conn.executescript(SCHEMA)
            for table, column, ddl in ADDED_COLUMNS:
                cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                if column not in cols:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            self.conn.commit()
        if data_dir is None and path == ":memory:":
            data_dir = tempfile.mkdtemp(prefix="agentgen-data-")
            weakref.finalize(self, shutil.rmtree, data_dir, True)  # sandboxes clean up after themselves
        elif data_dir is None:
            data_dir = f"{path}.data"
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        self.conn.close()

    def _insert(self, sql: str, params: tuple) -> int:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur.lastrowid

    def _update(self, sql: str, params: tuple) -> None:
        self.conn.execute(sql, params)
        self.conn.commit()

    # --- customers, requirements, projects -------------------------------------------

    def get_or_create_customer(self, name: str) -> int:
        row = self.conn.execute("SELECT id FROM customers WHERE name = ?", (name,)).fetchone()
        if row:
            return row["id"]
        return self._insert("INSERT INTO customers (name, created_at) VALUES (?, ?)", (name, _now()))

    def add_requirements(self, customer_id: int, text: str) -> int:
        return self._insert(
            "INSERT INTO requirements (customer_id, text, created_at) VALUES (?, ?, ?)", (customer_id, text, _now())
        )

    def create_project(self, customer_id: int, title: str, intake: str, intake_fields: dict | None = None) -> int:
        return self._insert(
            """INSERT INTO projects (customer_id, title, intake, intake_json, stage, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'intake', ?, ?)""",
            (customer_id, title, intake, json.dumps(intake_fields, ensure_ascii=False) if intake_fields else None,
             _now(), _now()),
        )

    def project(self, project_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise KeyError(f"No project {project_id}")
        return row

    def list_projects(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT p.id, p.title, p.stage, p.agent_name, c.name AS customer
               FROM projects p JOIN customers c ON c.id = p.customer_id ORDER BY p.id"""
        ).fetchall()

    def list_projects_for(self, customer_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT id, title, stage, agent_name, created_at, updated_at FROM projects
               WHERE customer_id = ? ORDER BY id DESC""", (customer_id,)
        ).fetchall()

    def update_project(self, project_id: int, **fields) -> None:
        allowed = {"questions_json", "answers_json", "analysis_json", "agent_name", "stage", "intake", "intake_json",
                   "amendments", "title", "questions_provider", "analysis_provider", "design_provider"}
        if not fields or set(fields) - allowed:
            raise ValueError(f"Can only update {sorted(allowed)}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._update(f"UPDATE projects SET {sets}, updated_at = ? WHERE id = ?", (*fields.values(), _now(), project_id))

    # --- agents & versions -------------------------------------------------------------

    def agent(self, name: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM agents WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise KeyError(f"No agent named {name!r}")
        return row

    def agent_by_id(self, agent_id: int) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()

    def list_agents(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT a.name, c.name AS customer, a.deployed_version,
                      (SELECT MAX(version) FROM agent_versions v WHERE v.agent_id = a.id) AS latest_version
               FROM agents a JOIN customers c ON c.id = a.customer_id ORDER BY a.name"""
        ).fetchall()

    def save_version(self, customer_id: int, spec: AgentSpec, requirements_id: int | None = None,
                     origin: str = "generated", provider: str | None = None, note: str | None = None) -> int:
        """Store a spec as the next version of its agent (creating the agent if new).
        origin: generated | edited | imported | restored | fixed | tests_added; provider: which model
        provider produced it (None for human edits)."""
        row = self.conn.execute("SELECT * FROM agents WHERE name = ?", (spec.name,)).fetchone()
        if row is not None and row["customer_id"] != customer_id:
            # Agent names are global; never reveal or reuse another organisation's agent.
            # A random suffix, so the name reveals nothing about other organisations' agents.
            base = spec.name[:56]
            while row is not None and row["customer_id"] != customer_id:
                spec.name = f"{base}-{secrets.token_hex(3)}"
                row = self.conn.execute("SELECT * FROM agents WHERE name = ?", (spec.name,)).fetchone()
        if row is None:
            agent_id = self._insert(
                "INSERT INTO agents (customer_id, name, created_at) VALUES (?, ?, ?)", (customer_id, spec.name, _now())
            )
        else:
            agent_id = row["id"]
        latest = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM agent_versions WHERE agent_id = ?", (agent_id,)
        ).fetchone()["v"]
        version = latest + 1
        self._insert(
            """INSERT INTO agent_versions (agent_id, version, requirements_id, spec_json, status, created_at, origin,
               provider, note) VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?)""",
            (agent_id, version, requirements_id, spec.model_dump_json(), _now(), origin, provider, note),
        )
        return version

    def _version_row(self, agent_name: str, version: int | None) -> sqlite3.Row:
        agent = self.agent(agent_name)
        if version is None:
            row = self.conn.execute(
                "SELECT * FROM agent_versions WHERE agent_id = ? ORDER BY version DESC LIMIT 1", (agent["id"],)
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM agent_versions WHERE agent_id = ? AND version = ?", (agent["id"], version)
            ).fetchone()
        if row is None:
            raise KeyError(f"No version {version} of agent {agent_name!r}")
        return row

    def load_spec(self, agent_name: str, version: int | None = None) -> tuple[AgentSpec, int]:
        """Load a spec. version=None means the latest version."""
        row = self._version_row(agent_name, version)
        return AgentSpec.from_json(row["spec_json"]), row["version"]

    def version_status(self, agent_name: str, version: int) -> str:
        return self._version_row(agent_name, version)["status"]

    def record_test_run(self, agent_name: str, version: int, pass_rate: float, passed: bool, report: dict) -> None:
        row = self._version_row(agent_name, version)
        self._insert(
            "INSERT INTO test_runs (agent_version_id, pass_rate, passed, report_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (row["id"], pass_rate, int(passed), json.dumps(report), _now()),
        )
        self._update("UPDATE agent_versions SET status = ? WHERE id = ?", ("passed" if passed else "failed", row["id"]))

    def latest_test_report(self, agent_name: str, version: int) -> dict | None:
        row = self._version_row(agent_name, version)
        tr = self.conn.execute(
            "SELECT report_json FROM test_runs WHERE agent_version_id = ? ORDER BY id DESC LIMIT 1", (row["id"],)
        ).fetchone()
        return json.loads(tr["report_json"]) if tr else None

    def last_passed_version(self, agent_name: str, before: int) -> int | None:
        agent = self.agent(agent_name)
        row = self.conn.execute(
            """SELECT MAX(version) AS v FROM agent_versions
               WHERE agent_id = ? AND version < ? AND status = 'passed'""",
            (agent["id"], before),
        ).fetchone()
        return row["v"]

    def approve_version(self, agent_name: str, version: int, approved_by: str, note: str) -> None:
        if not approved_by.strip():
            raise ValueError("An approval needs the approver's name")
        row = self._version_row(agent_name, version)
        if row["status"] != "passed":
            raise ValueError(f"Version {version} has status {row['status']!r}; approve only versions that passed tests")
        self._insert(
            "INSERT INTO approvals (agent_version_id, approved_by, note, created_at) VALUES (?, ?, ?, ?)",
            (row["id"], approved_by, note, _now()),
        )

    def approval_for(self, agent_name: str, version: int) -> sqlite3.Row | None:
        row = self._version_row(agent_name, version)
        return self.conn.execute(
            "SELECT * FROM approvals WHERE agent_version_id = ? ORDER BY id DESC LIMIT 1", (row["id"],)
        ).fetchone()

    def deploy(self, agent_name: str, version: int, requires_approval: bool = False) -> None:
        """Point the agent at a version. Only versions that passed their tests can be deployed,
        and consequential agents also need a recorded approval. Deploying an older passed
        version is how rollback works."""
        status = self.version_status(agent_name, version)
        if status != "passed":
            raise ValueError(f"Version {version} of {agent_name!r} has status {status!r}; only passed versions deploy")
        if requires_approval and self.approval_for(agent_name, version) is None:
            raise ValueError(
                f"Version {version} of {agent_name!r} can take consequential actions and needs a recorded approval "
                f"first (agentgen approve {agent_name} --version {version} --by NAME)"
            )
        self._update("UPDATE agents SET deployed_version = ? WHERE name = ?", (version, agent_name))
        self.audit(self.agent(agent_name)["id"], None, "deployed", {"version": version})

    def undeploy(self, agent_name: str) -> None:
        """Stop the agent: no version is live, schedules and events stop creating jobs."""
        agent = self.agent(agent_name)
        self._update("UPDATE agents SET deployed_version = NULL WHERE id = ?", (agent["id"],))
        self._update("UPDATE jobs SET status = 'cancelled' WHERE agent_id = ? AND status = 'pending'", (agent["id"],))
        self.audit(agent["id"], None, "undeployed", {"version": agent["deployed_version"]})

    def deployed_spec(self, agent_name: str) -> tuple[AgentSpec, int]:
        version = self.agent(agent_name)["deployed_version"]
        if version is None:
            raise ValueError(f"Agent {agent_name!r} has no deployed version")
        return self.load_spec(agent_name, version)

    def deployed_agents(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM agents WHERE deployed_version IS NOT NULL").fetchall()

    # --- knowledge & datasets -----------------------------------------------------------

    def add_knowledge(self, agent_id: int, title: str, content: str) -> int:
        return self._insert(
            "INSERT INTO knowledge_docs (agent_id, title, content, created_at) VALUES (?, ?, ?, ?)",
            (agent_id, title, content, _now()),
        )

    def knowledge(self, agent_id: int) -> list[dict]:
        """Documents an agent can read: its own knowledge docs plus its organisation's library."""
        agent = self.agent_by_id(agent_id)
        own = self.conn.execute(
            "SELECT id, title, content FROM knowledge_docs WHERE agent_id = ? ORDER BY id", (agent_id,)
        ).fetchall()
        library = self.conn.execute(
            "SELECT id, title, content FROM documents WHERE customer_id = ? ORDER BY id", (agent["customer_id"],)
        ).fetchall() if agent else []
        return ([{"id": f"K{r['id']}", "title": r["title"], "content": r["content"]} for r in own]
                + [{"id": f"D{r['id']}", "title": r["title"], "content": r["content"]} for r in library])

    def add_document(self, customer_id: int, title: str, content: str) -> int:
        return self._insert(
            "INSERT INTO documents (customer_id, title, content, created_at) VALUES (?, ?, ?, ?)",
            (customer_id, title, content, _now()),
        )

    def documents(self, customer_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, title, LENGTH(content) AS size, created_at FROM documents WHERE customer_id = ? ORDER BY id",
            (customer_id,),
        ).fetchall()

    def delete_document(self, customer_id: int, doc_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM documents WHERE id = ? AND customer_id = ?", (doc_id, customer_id))
        self.conn.commit()
        return cur.rowcount > 0

    def customer_data_path(self, customer_id: int) -> Path:
        return self.data_dir / f"customer_{customer_id}.sqlite"

    def register_dataset(self, customer_id: int, name: str, columns: list[str], row_count: int) -> None:
        self._update(
            """INSERT INTO datasets (customer_id, name, columns_json, row_count, created_at) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (customer_id, name) DO UPDATE SET columns_json = excluded.columns_json,
               row_count = excluded.row_count, created_at = excluded.created_at""",
            (customer_id, name, json.dumps(columns), row_count, _now()),
        )

    def datasets(self, customer_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT name, columns_json, row_count FROM datasets WHERE customer_id = ? ORDER BY name", (customer_id,)
        ).fetchall()

    # --- runs, audit and agent outputs ---------------------------------------------------

    def start_run(self, agent_id: int, version: int, input_text: str, trigger_source: str = "manual",
                  provider: str | None = None, simulated: bool = False) -> int:
        return self._insert(
            """INSERT INTO runs (agent_id, version, input, trigger_source, provider, simulated, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (agent_id, version, input_text, trigger_source, provider, int(simulated), _now()),
        )

    def finish_run(self, run_id: int, output: str, stop_reason: str, input_tokens: int, output_tokens: int,
                   duration_ms: int | None = None, error: str | None = None) -> None:
        self._update(
            """UPDATE runs SET output = ?, stop_reason = ?, input_tokens = ?, output_tokens = ?, duration_ms = ?,
               error = ? WHERE id = ?""",
            (output, stop_reason, input_tokens, output_tokens, duration_ms, error, run_id),
        )

    def audit(self, agent_id: int, run_id: int | None, event: str, data: dict) -> None:
        self._insert(
            "INSERT INTO audit_log (agent_id, run_id, event, data_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (agent_id, run_id, event, json.dumps(data, ensure_ascii=False, default=str), _now()),
        )

    def add_record(self, agent_id: int, record_type: str, data: dict, status: str = "open") -> int:
        return self._insert(
            "INSERT INTO records (agent_id, table_name, data_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (agent_id, record_type, json.dumps(data, ensure_ascii=False), status, _now(), _now()),
        )

    def get_record(self, agent_id: int, record_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM records WHERE agent_id = ? AND id = ?", (agent_id, record_id)
        ).fetchone()

    def update_record(self, agent_id: int, record_id: int, changes: dict, status: str | None) -> bool:
        row = self.get_record(agent_id, record_id)
        if row is None:
            return False
        data = {**json.loads(row["data_json"]), **changes}
        self._update(
            "UPDATE records SET data_json = ?, status = ?, updated_at = ? WHERE id = ? AND agent_id = ?",
            (json.dumps(data, ensure_ascii=False), status or row["status"], _now(), record_id, agent_id),
        )
        return True

    def search_records(self, agent_id: int, record_type: str, text: str, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM records WHERE agent_id = ? AND table_name = ? AND data_json LIKE ?
               ORDER BY id DESC LIMIT ?""",
            (agent_id, record_type, f"%{text}%", limit),
        ).fetchall()

    def add_outbox(self, agent_id: int, status: str, recipient: str, subject: str, body: str,
                   channel: str = "email") -> int:
        return self._insert(
            """INSERT INTO outbox (agent_id, status, channel, recipient, subject, body, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (agent_id, status, channel, recipient, subject, body, _now()),
        )

    def add_escalation(self, agent_id: int, reason: str, details: str) -> int:
        return self._insert(
            "INSERT INTO escalations (agent_id, reason, details, created_at) VALUES (?, ?, ?, ?)",
            (agent_id, reason, details, _now()),
        )

    # --- memory --------------------------------------------------------------------------

    def remember(self, agent_id: int, key: str, content: str, retention_days: int) -> None:
        expires = (utcnow() + timedelta(days=retention_days)).isoformat()
        self._update(
            """INSERT INTO memory (agent_id, key, content, expires_at, created_at) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (agent_id, key) DO UPDATE SET content = excluded.content,
               expires_at = excluded.expires_at, created_at = excluded.created_at""",
            (agent_id, key, content, expires, _now()),
        )

    def recall(self, agent_id: int, query: str, limit: int = 5) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT key, content, created_at FROM memory
               WHERE agent_id = ? AND expires_at > ? AND (key LIKE ? OR content LIKE ?)
               ORDER BY created_at DESC LIMIT ?""",
            (agent_id, _now(), f"%{query}%", f"%{query}%", limit),
        ).fetchall()

    def purge_expired_memory(self) -> int:
        cur = self.conn.execute("DELETE FROM memory WHERE expires_at <= ?", (_now(),))
        self.conn.commit()
        return cur.rowcount

    # --- jobs & queued approvals ---------------------------------------------------------

    def enqueue_job(self, agent_id: int, input_text: str, source: str, due_at: datetime | None = None) -> int:
        due = (due_at or utcnow()).isoformat()
        return self._insert(
            "INSERT INTO jobs (agent_id, due_at, input, source, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
            (agent_id, due, input_text, source, _now()),
        )

    def due_jobs(self, now: datetime | None = None) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM jobs WHERE status = 'pending' AND due_at <= ? ORDER BY due_at, id",
            ((now or utcnow()).isoformat(),),
        ).fetchall()

    def pending_job_exists(self, agent_id: int, source: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM jobs WHERE agent_id = ? AND source = ? AND status IN ('pending', 'running')",
            (agent_id, source),
        ).fetchone() is not None

    def last_job(self, agent_id: int, source: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM jobs WHERE agent_id = ? AND source = ? ORDER BY due_at DESC LIMIT 1", (agent_id, source)
        ).fetchone()

    def update_job(self, job_id: int, **fields) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._update(f"UPDATE jobs SET {sets} WHERE id = ?", (*fields.values(), job_id))

    def queue_action(self, agent_id: int, run_id: int | None, tool: str, tool_input: dict,
                     idem_key: str | None = None) -> int:
        """Queue an action for a person. With an idempotency key, the same action requested again
        (e.g. by a retried job) returns the existing entry instead of queuing a duplicate."""
        if idem_key:
            row = self.conn.execute("SELECT id FROM pending_actions WHERE agent_id = ? AND idem_key = ?",
                                    (agent_id, idem_key)).fetchone()
            if row:
                return row["id"]
        return self._insert(
            """INSERT INTO pending_actions (agent_id, run_id, tool, input_json, status, idem_key, created_at)
               VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
            (agent_id, run_id, tool, json.dumps(tool_input, ensure_ascii=False), idem_key, _now()),
        )

    def action_by_key(self, agent_id: int, idem_key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM pending_actions WHERE agent_id = ? AND idem_key = ?",
                                 (agent_id, idem_key)).fetchone()

    def claim_action(self, action_id: int, decided_by: str) -> bool:
        """Atomically move a pending action to 'executing'. Only one caller can win, so two people
        approving at the same moment can't run it twice."""
        cur = self.conn.execute(
            "UPDATE pending_actions SET status = 'executing', decided_by = ? WHERE id = ? AND status = 'pending'",
            (decided_by, action_id))
        self.conn.commit()
        return cur.rowcount == 1

    def executed_result(self, agent_id: int, idem_key: str) -> str | None:
        row = self.conn.execute("SELECT result FROM tool_executions WHERE agent_id = ? AND idem_key = ?",
                                (agent_id, idem_key)).fetchone()
        return row["result"] if row else None

    def record_execution(self, agent_id: int, idem_key: str, tool: str, result: str) -> None:
        self._update(
            """INSERT OR IGNORE INTO tool_executions (agent_id, idem_key, tool, result, created_at)
               VALUES (?, ?, ?, ?, ?)""", (agent_id, idem_key, tool, result, _now()))

    def pending_actions(self, agent_id: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT p.*, a.name AS agent FROM pending_actions p JOIN agents a ON a.id = p.agent_id WHERE p.status = 'pending'"
        params: tuple = ()
        if agent_id is not None:
            sql += " AND p.agent_id = ?"
            params = (agent_id,)
        return self.conn.execute(sql + " ORDER BY p.id", params).fetchall()

    def action(self, action_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM pending_actions WHERE id = ?", (action_id,)).fetchone()
        if row is None:
            raise KeyError(f"No queued action {action_id}")
        return row

    def decide_action(self, action_id: int, status: str, decided_by: str, result: str) -> None:
        self._update(
            "UPDATE pending_actions SET status = ?, decided_by = ?, result = ? WHERE id = ?",
            (status, decided_by, result, action_id),
        )

    # --- test sandboxes -------------------------------------------------------------------

    def sandbox_for(self, agent_name: str) -> tuple["Store", int]:
        """An in-memory copy holding only this agent's knowledge, records and datasets, so tests
        can let the agent act freely without touching real data. Returns (sandbox, agent_id)."""
        agent = self.agent(agent_name)
        sandbox = Store(":memory:")
        customer_id = sandbox.get_or_create_customer("sandbox")
        agent_id = sandbox._insert(
            "INSERT INTO agents (customer_id, name, created_at) VALUES (?, ?, ?)", (customer_id, agent_name, _now())
        )
        for doc in self.knowledge(agent["id"]):  # agent docs and the organisation's library
            sandbox.add_knowledge(agent_id, doc["title"], doc["content"])
        for rec in self.conn.execute("SELECT * FROM records WHERE agent_id = ?", (agent["id"],)):
            sandbox.add_record(agent_id, rec["table_name"], json.loads(rec["data_json"]), rec["status"])
        source = self.customer_data_path(agent["customer_id"])
        if source.exists():
            shutil.copy(source, sandbox.customer_data_path(customer_id))
        for ds in self.datasets(agent["customer_id"]):
            sandbox.register_dataset(customer_id, ds["name"], json.loads(ds["columns_json"]), ds["row_count"])
        return sandbox, agent_id

    # --- users & sessions -----------------------------------------------------------------

    def create_user(self, customer_id: int, email: str, name: str, password_hash: str, role: str) -> int:
        return self._insert(
            "INSERT INTO users (customer_id, email, name, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (customer_id, email.lower(), name, password_hash, role, _now()),
        )

    def user_by_email(self, email: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM users WHERE email = ?", (email.lower(),)).fetchone()

    def customer_exists(self, name: str) -> bool:
        return self.conn.execute("SELECT 1 FROM customers WHERE name = ?", (name,)).fetchone() is not None

    def create_session(self, token_hash: str, user_id: int, expires_at: str) -> None:
        self._update("INSERT INTO sessions (token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
                     (token_hash, user_id, expires_at, _now()))

    def session_user(self, token_hash: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """SELECT u.*, c.name AS organization FROM sessions s JOIN users u ON u.id = s.user_id
               JOIN customers c ON c.id = u.customer_id WHERE s.token_hash = ? AND s.expires_at > ?""",
            (token_hash, _now()),
        ).fetchone()

    def delete_session(self, token_hash: str) -> None:
        self._update("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    # --- organisation settings & spend ----------------------------------------------------

    def monthly_budget(self, customer_id: int, default: float) -> float:
        row = self.conn.execute("SELECT monthly_budget_usd FROM org_settings WHERE customer_id = ?",
                                (customer_id,)).fetchone()
        return row["monthly_budget_usd"] if row else default

    def set_monthly_budget(self, customer_id: int, usd: float) -> None:
        self._update(
            """INSERT INTO org_settings (customer_id, monthly_budget_usd, updated_at) VALUES (?, ?, ?)
               ON CONFLICT (customer_id) DO UPDATE SET monthly_budget_usd = excluded.monthly_budget_usd,
               updated_at = excluded.updated_at""",
            (customer_id, usd, _now()),
        )

    def record_usage(self, customer_id: int, purpose: str, model: str, input_tokens: int, output_tokens: int,
                     usd: float, provider: str = "anthropic", simulated: bool = False) -> None:
        self._insert(
            """INSERT INTO model_usage (customer_id, purpose, model, input_tokens, output_tokens, usd, provider,
               simulated, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (customer_id, purpose, model, input_tokens, output_tokens, usd, provider, int(simulated), _now()),
        )

    def month_spend(self, customer_id: int, simulated: bool | None = False) -> float:
        """Estimated spend this calendar month. simulated=False: real model usage only (what the
        budget limits); True: test-mode usage only; None: both."""
        month_start = utcnow().replace(day=1, hour=0, minute=0, second=0).isoformat()
        sql = "SELECT COALESCE(SUM(usd), 0) FROM model_usage WHERE customer_id = ? AND created_at >= ?"
        params: list = [customer_id, month_start]
        if simulated is not None:
            sql += " AND simulated = ?"
            params.append(int(simulated))
        return self.conn.execute(sql, params).fetchone()[0]

    def usage_by_purpose(self, customer_id: int, since: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT purpose, provider, MAX(simulated) AS simulated, COUNT(*) AS calls,
                      SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, SUM(usd) AS usd
               FROM model_usage WHERE customer_id = ? AND created_at >= ? GROUP BY purpose, provider
               ORDER BY usd DESC""",
            (customer_id, since),
        ).fetchall()

    # --- background operations ------------------------------------------------------------

    def create_operation(self, customer_id: int, kind: str, target: dict, created_by: str) -> int:
        return self._insert(
            """INSERT INTO operations (customer_id, kind, target_json, status, created_by, created_at, updated_at)
               VALUES (?, ?, ?, 'queued', ?, ?, ?)""",
            (customer_id, kind, json.dumps(target), created_by, _now(), _now()),
        )

    def update_operation(self, op_id: int, status: str, result: dict | None = None, error: str | None = None) -> None:
        self._update(
            "UPDATE operations SET status = ?, result_json = ?, error = ?, updated_at = ? WHERE id = ?",
            (status, json.dumps(result, default=str) if result is not None else None, error, _now(), op_id),
        )

    def operation(self, op_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM operations WHERE id = ?", (op_id,)).fetchone()

    def operations_for(self, customer_id: int, kind: str | None = None, target_key: str | None = None,
                       target_value=None, limit: int = 20) -> list[sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT * FROM operations WHERE customer_id = ? ORDER BY id DESC LIMIT 200", (customer_id,)
        ).fetchall()
        out = []
        for r in rows:
            if kind and r["kind"] != kind:
                continue
            if target_key and json.loads(r["target_json"]).get(target_key) != target_value:
                continue
            out.append(r)
        return out[:limit]

    def fail_interrupted_operations(self) -> None:
        """Operations still running when the server stopped will never finish."""
        self._update("UPDATE operations SET status = 'failed', error = 'Interrupted by a server restart', "
                     "updated_at = ? WHERE status IN ('queued', 'running')", (_now(),))

    # --- organisation-scoped queries for the web app ---------------------------------------

    def agents_for(self, customer_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT a.*, (SELECT MAX(version) FROM agent_versions v WHERE v.agent_id = a.id) AS latest_version,
                      (SELECT status FROM agent_versions v WHERE v.agent_id = a.id ORDER BY version DESC LIMIT 1)
                          AS latest_status,
                      (SELECT MAX(created_at) FROM runs r WHERE r.agent_id = a.id) AS last_run_at
               FROM agents a WHERE a.customer_id = ? ORDER BY a.name""",
            (customer_id,),
        ).fetchall()

    def agent_scoped(self, customer_id: int, agent_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM agents WHERE id = ? AND customer_id = ?",
                                 (agent_id, customer_id)).fetchone()

    def versions(self, agent_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT v.id, v.version, v.status, v.created_at, v.origin, v.provider, v.note,
                      (SELECT approved_by FROM approvals ap WHERE ap.agent_version_id = v.id ORDER BY id DESC LIMIT 1)
                          AS approved_by,
                      (SELECT created_at FROM approvals ap WHERE ap.agent_version_id = v.id ORDER BY id DESC LIMIT 1)
                          AS approved_at,
                      (SELECT pass_rate FROM test_runs t WHERE t.agent_version_id = v.id ORDER BY id DESC LIMIT 1)
                          AS pass_rate
               FROM agent_versions v WHERE v.agent_id = ? ORDER BY v.version DESC""",
            (agent_id,),
        ).fetchall()

    def test_reports(self, agent_name: str, version: int, limit: int = 10) -> list[sqlite3.Row]:
        row = self._version_row(agent_name, version)
        return self.conn.execute(
            "SELECT id, pass_rate, passed, report_json, created_at FROM test_runs WHERE agent_version_id = ? "
            "ORDER BY id DESC LIMIT ?", (row["id"], limit),
        ).fetchall()

    def runs_for(self, customer_id: int, agent_id: int | None = None, limit: int = 100) -> list[sqlite3.Row]:
        sql = """SELECT r.*, a.name AS agent FROM runs r JOIN agents a ON a.id = r.agent_id
                 WHERE a.customer_id = ?"""
        params: list = [customer_id]
        if agent_id is not None:
            sql += " AND r.agent_id = ?"
            params.append(agent_id)
        return self.conn.execute(sql + " ORDER BY r.id DESC LIMIT ?", (*params, limit)).fetchall()

    def run_scoped(self, customer_id: int, run_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            """SELECT r.*, a.name AS agent FROM runs r JOIN agents a ON a.id = r.agent_id
               WHERE r.id = ? AND a.customer_id = ?""", (run_id, customer_id),
        ).fetchone()

    def run_events(self, run_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM audit_log WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()

    def actions_for(self, customer_id: int, status: str | None = "pending") -> list[sqlite3.Row]:
        sql = """SELECT p.*, a.name AS agent FROM pending_actions p JOIN agents a ON a.id = p.agent_id
                 WHERE a.customer_id = ?"""
        params: list = [customer_id]
        if status:
            sql += " AND p.status = ?"
            params.append(status)
        return self.conn.execute(sql + " ORDER BY p.id DESC LIMIT 200", params).fetchall()

    def jobs_for(self, customer_id: int, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT j.*, a.name AS agent FROM jobs j JOIN agents a ON a.id = j.agent_id
               WHERE a.customer_id = ? ORDER BY j.id DESC LIMIT ?""", (customer_id, limit),
        ).fetchall()

    def outputs_for_run(self, agent_id: int, since: str) -> dict:
        """What an agent produced since a time (records, outbox, escalations) - for run details."""
        q = lambda t: [dict(r) for r in self.conn.execute(
            f"SELECT * FROM {t} WHERE agent_id = ? AND created_at >= ? ORDER BY id", (agent_id, since))]
        return {"records": q("records"), "outbox": q("outbox"), "escalations": q("escalations")}
