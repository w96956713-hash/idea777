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
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _now() -> str:
    return utcnow().isoformat()


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class Store:
    def __init__(self, path: str = "agentgen.db", data_dir: str | None = None):
        self.path = path
        self.conn = connect(path)
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

    def create_project(self, customer_id: int, title: str, intake: str) -> int:
        return self._insert(
            """INSERT INTO projects (customer_id, title, intake, stage, created_at, updated_at)
               VALUES (?, ?, ?, 'intake', ?, ?)""",
            (customer_id, title, intake, _now(), _now()),
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

    def update_project(self, project_id: int, **fields) -> None:
        allowed = {"questions_json", "answers_json", "analysis_json", "agent_name", "stage"}
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

    def save_version(self, customer_id: int, spec: AgentSpec, requirements_id: int | None = None) -> int:
        """Store a spec as the next version of its agent (creating the agent if new)."""
        row = self.conn.execute("SELECT * FROM agents WHERE name = ?", (spec.name,)).fetchone()
        if row is None:
            agent_id = self._insert(
                "INSERT INTO agents (customer_id, name, created_at) VALUES (?, ?, ?)", (customer_id, spec.name, _now())
            )
        else:
            if row["customer_id"] != customer_id:
                raise ValueError(f"Agent {spec.name!r} belongs to another customer")
            agent_id = row["id"]
        latest = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM agent_versions WHERE agent_id = ?", (agent_id,)
        ).fetchone()["v"]
        version = latest + 1
        self._insert(
            """INSERT INTO agent_versions (agent_id, version, requirements_id, spec_json, status, created_at)
               VALUES (?, ?, ?, ?, 'draft', ?)""",
            (agent_id, version, requirements_id, spec.model_dump_json(), _now()),
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

    def knowledge(self, agent_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, title, content FROM knowledge_docs WHERE agent_id = ? ORDER BY id", (agent_id,)
        ).fetchall()

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

    def start_run(self, agent_id: int, version: int, input_text: str, trigger_source: str = "manual") -> int:
        return self._insert(
            "INSERT INTO runs (agent_id, version, input, trigger_source, created_at) VALUES (?, ?, ?, ?, ?)",
            (agent_id, version, input_text, trigger_source, _now()),
        )

    def finish_run(self, run_id: int, output: str, stop_reason: str, input_tokens: int, output_tokens: int) -> None:
        self._update(
            "UPDATE runs SET output = ?, stop_reason = ?, input_tokens = ?, output_tokens = ? WHERE id = ?",
            (output, stop_reason, input_tokens, output_tokens, run_id),
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

    def queue_action(self, agent_id: int, run_id: int | None, tool: str, tool_input: dict) -> int:
        return self._insert(
            """INSERT INTO pending_actions (agent_id, run_id, tool, input_json, status, created_at)
               VALUES (?, ?, ?, ?, 'pending', ?)""",
            (agent_id, run_id, tool, json.dumps(tool_input, ensure_ascii=False), _now()),
        )

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
        for doc in self.knowledge(agent["id"]):
            sandbox.add_knowledge(agent_id, doc["title"], doc["content"])
        for rec in self.conn.execute("SELECT * FROM records WHERE agent_id = ?", (agent["id"],)):
            sandbox.add_record(agent_id, rec["table_name"], json.loads(rec["data_json"]), rec["status"])
        source = self.customer_data_path(agent["customer_id"])
        if source.exists():
            shutil.copy(source, sandbox.customer_data_path(customer_id))
        for ds in self.datasets(agent["customer_id"]):
            sandbox.register_dataset(customer_id, ds["name"], json.loads(ds["columns_json"]), ds["row_count"])
        return sandbox, agent_id
