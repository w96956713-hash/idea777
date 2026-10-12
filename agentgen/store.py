"""Database layer. Everything the platform knows lives here.

Uses SQLite by default (a single file, zero setup). The schema is plain SQL that also
runs on PostgreSQL, so production can move to a hosted Postgres (e.g. Supabase or Neon in
an EU region) by reimplementing `connect()` without touching the rest of the code.

Every customer-owned row carries `agent_id` (and through it `customer_id`), and every
query used by agents filters on it - this is the tenant isolation boundary.
"""

import json
import sqlite3
from datetime import datetime, timezone

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
CREATE TABLE IF NOT EXISTS knowledge_docs (
    id INTEGER PRIMARY KEY,
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
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
    table_name TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY,
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    status TEXT NOT NULL,  -- draft | sent
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
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class Store:
    def __init__(self, path: str = "agentgen.db"):
        self.path = path
        self.conn = connect(path)
        self.conn.executescript(SCHEMA)

    def _insert(self, sql: str, params: tuple) -> int:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur.lastrowid

    # --- customers & requirements -------------------------------------------------

    def get_or_create_customer(self, name: str) -> int:
        row = self.conn.execute("SELECT id FROM customers WHERE name = ?", (name,)).fetchone()
        if row:
            return row["id"]
        return self._insert("INSERT INTO customers (name, created_at) VALUES (?, ?)", (name, _now()))

    def add_requirements(self, customer_id: int, text: str) -> int:
        return self._insert(
            "INSERT INTO requirements (customer_id, text, created_at) VALUES (?, ?, ?)",
            (customer_id, text, _now()),
        )

    # --- agents & versions ---------------------------------------------------------

    def agent(self, name: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM agents WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise KeyError(f"No agent named {name!r}")
        return row

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
                "INSERT INTO agents (customer_id, name, created_at) VALUES (?, ?, ?)",
                (customer_id, spec.name, _now()),
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
        return AgentSpec.model_validate_json(row["spec_json"]), row["version"]

    def version_status(self, agent_name: str, version: int) -> str:
        return self._version_row(agent_name, version)["status"]

    def record_test_run(self, agent_name: str, version: int, pass_rate: float, passed: bool, report: dict) -> None:
        row = self._version_row(agent_name, version)
        self._insert(
            "INSERT INTO test_runs (agent_version_id, pass_rate, passed, report_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (row["id"], pass_rate, int(passed), json.dumps(report), _now()),
        )
        self.conn.execute(
            "UPDATE agent_versions SET status = ? WHERE id = ?", ("passed" if passed else "failed", row["id"])
        )
        self.conn.commit()

    def deploy(self, agent_name: str, version: int) -> None:
        """Point the agent at a version. Only versions that passed their tests can be deployed;
        deploying an older passed version is how rollback works."""
        status = self.version_status(agent_name, version)
        if status != "passed":
            raise ValueError(f"Version {version} of {agent_name!r} has status {status!r}; only passed versions deploy")
        self.conn.execute("UPDATE agents SET deployed_version = ? WHERE name = ?", (version, agent_name))
        self.conn.commit()

    def deployed_spec(self, agent_name: str) -> tuple[AgentSpec, int]:
        version = self.agent(agent_name)["deployed_version"]
        if version is None:
            raise ValueError(f"Agent {agent_name!r} has no deployed version")
        return self.load_spec(agent_name, version)

    # --- knowledge -----------------------------------------------------------------

    def add_knowledge(self, agent_id: int, title: str, content: str) -> int:
        return self._insert(
            "INSERT INTO knowledge_docs (agent_id, title, content, created_at) VALUES (?, ?, ?, ?)",
            (agent_id, title, content, _now()),
        )

    def knowledge(self, agent_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, title, content FROM knowledge_docs WHERE agent_id = ? ORDER BY id", (agent_id,)
        ).fetchall()

    # --- runs, audit and agent outputs -----------------------------------------------

    def start_run(self, agent_id: int, version: int, input_text: str) -> int:
        return self._insert(
            "INSERT INTO runs (agent_id, version, input, created_at) VALUES (?, ?, ?, ?)",
            (agent_id, version, input_text, _now()),
        )

    def finish_run(self, run_id: int, output: str, stop_reason: str, input_tokens: int, output_tokens: int) -> None:
        self.conn.execute(
            "UPDATE runs SET output = ?, stop_reason = ?, input_tokens = ?, output_tokens = ? WHERE id = ?",
            (output, stop_reason, input_tokens, output_tokens, run_id),
        )
        self.conn.commit()

    def audit(self, agent_id: int, run_id: int | None, event: str, data: dict) -> None:
        self._insert(
            "INSERT INTO audit_log (agent_id, run_id, event, data_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (agent_id, run_id, event, json.dumps(data, ensure_ascii=False, default=str), _now()),
        )

    def add_record(self, agent_id: int, table_name: str, data: dict) -> int:
        return self._insert(
            "INSERT INTO records (agent_id, table_name, data_json, created_at) VALUES (?, ?, ?, ?)",
            (agent_id, table_name, json.dumps(data, ensure_ascii=False), _now()),
        )

    def add_outbox(self, agent_id: int, status: str, recipient: str, subject: str, body: str) -> int:
        return self._insert(
            "INSERT INTO outbox (agent_id, status, recipient, subject, body, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (agent_id, status, recipient, subject, body, _now()),
        )

    def add_escalation(self, agent_id: int, reason: str, details: str) -> int:
        return self._insert(
            "INSERT INTO escalations (agent_id, reason, details, created_at) VALUES (?, ?, ?, ?)",
            (agent_id, reason, details, _now()),
        )

    def usage_summary(self, agent_name: str) -> sqlite3.Row:
        agent = self.agent(agent_name)
        return self.conn.execute(
            """SELECT COUNT(*) AS runs, COALESCE(SUM(input_tokens), 0) AS input_tokens,
                      COALESCE(SUM(output_tokens), 0) AS output_tokens,
                      (SELECT COUNT(*) FROM escalations WHERE agent_id = ?) AS escalations
               FROM runs WHERE agent_id = ?""",
            (agent["id"], agent["id"]),
        ).fetchone()

    # --- test sandboxes --------------------------------------------------------------

    def sandbox_for(self, agent_name: str) -> tuple["Store", int]:
        """An in-memory copy holding only this agent and its knowledge, so tests can let the
        agent act freely without touching real data. Returns (sandbox, agent_id in sandbox)."""
        agent = self.agent(agent_name)
        sandbox = Store(":memory:")
        customer_id = sandbox.get_or_create_customer("sandbox")
        agent_id = sandbox._insert(
            "INSERT INTO agents (customer_id, name, created_at) VALUES (?, ?, ?)", (customer_id, agent_name, _now())
        )
        for doc in self.knowledge(agent["id"]):
            sandbox.add_knowledge(agent_id, doc["title"], doc["content"])
        return sandbox, agent_id
