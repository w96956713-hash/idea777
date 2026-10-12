"""The capability library: reusable, tested building blocks that agents are assembled from.

Each tool in a spec names a component and configures it. A component:
- declares a risk class (read < write_internal < write_external < external_comm < financial),
  which drives the approval policy and the release gate;
- builds the tool definition the model sees, tailored to the agent (e.g. query_data lists
  the agent's actual tables, http_request uses the endpoint's own parameter schema);
- validates its config, and runs against the agent's own, isolated data.

New agent types come from new combinations of components. A new integration is either a
configured `http_request` tool (no code) or a new Component subclass here.
"""

import ast
import ipaddress
import json
import operator
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import timedelta

from . import datasets
from .spec import RISK_ORDER, ToolSpec
from .store import Store, utcnow


@dataclass
class Context:
    store: Store
    agent_id: int
    customer_id: int
    tool: ToolSpec
    run_id: int | None = None
    sandbox: bool = False
    memory_retention_days: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def config(self) -> dict[str, str]:
        return self.tool.config_dict()


class ToolFailure(Exception):
    """A tool could not complete; the message is shown to the agent."""


_SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


def secret_env_name(customer_id: int, name: str) -> str:
    """Secrets are scoped per organisation: a spec names `CRM_TOKEN`, the server reads
    `AGENTGEN_ORG<id>_CRM_TOKEN`. A spec can never reach another organisation's secrets or the
    platform's own credentials (e.g. the model API key)."""
    return f"AGENTGEN_ORG{customer_id}_{name}"


def org_secret(customer_id: int, name: str) -> str | None:
    if not _SECRET_NAME.match(name or ""):
        return None
    return os.environ.get(secret_env_name(customer_id, name))


def _check_public_host(host: str) -> None:
    """Refuse requests to loopback, private, link-local or reserved addresses (SSRF)."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ToolFailure(f"cannot resolve host {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ToolFailure(f"requests to internal network addresses are not allowed ({host})")


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required if required is not None else list(properties),
        "additionalProperties": False,
    }


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _parse_json_object(text: str, field_name: str) -> dict:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ToolFailure(f"{field_name} is not valid JSON ({e})")
    if not isinstance(data, dict):
        raise ToolFailure(f"{field_name} must be a JSON object")
    return data


class Component:
    name: str
    summary: str  # what it does, for the designer and the agent
    risk: str = "read"
    config_keys: dict[str, str] = {}
    required_config: tuple[str, ...] = ()
    input_schema: dict = _schema({})
    strict: bool = True

    def risk_for(self, tool: ToolSpec) -> str:
        return self.risk

    def schema_for(self, tool: ToolSpec) -> dict:
        return self.input_schema

    def describe(self, tool: ToolSpec, ctx: Context) -> str:
        return self.summary

    def validate(self, tool: ToolSpec) -> list[str]:
        cfg = tool.config_dict()
        problems = [f"missing config '{k}'" for k in self.required_config if not cfg.get(k)]
        problems += [f"unknown config '{k}'" for k in cfg if k not in self.config_keys]
        return problems

    def tool_definition(self, tool: ToolSpec, ctx: Context) -> dict:
        description = self.describe(tool, ctx)
        if tool.purpose:
            description += f"\nUse in this role: {tool.purpose}"
        definition = {"name": tool.name, "description": description[:4000], "input_schema": self.schema_for(tool)}
        if self.strict:
            definition["strict"] = True
        return definition

    def run(self, args: dict, ctx: Context) -> str:
        raise NotImplementedError


# --- knowledge & documents ------------------------------------------------------------

_WORD = re.compile(r"\w+", re.UNICODE)


class KnowledgeSearch(Component):
    name = "knowledge_search"
    summary = ("Search the company's documents (policies, procedures, price lists, manuals). Returns the most "
               "relevant passages with document titles and ids.")
    config_keys = {"max_results": "Passages to return (default 4)."}
    input_schema = _schema({"query": {"type": "string", "description": "What to look for."}})

    def run(self, args, ctx):
        terms = {w.lower() for w in _WORD.findall(args["query"]) if len(w) > 2}
        scored = []
        for doc in ctx.store.knowledge(ctx.agent_id):
            title = doc["title"].lower()
            for para in [p.strip() for p in doc["content"].split("\n\n") if p.strip()]:
                words = [w.lower() for w in _WORD.findall(para)]
                score = sum(sum(1 for w in words if w == t or (len(t) > 4 and w.startswith(t[:-1]))) for t in terms)
                score += 2 * sum(t in title for t in terms)
                if score:
                    scored.append((score, doc["id"], doc["title"], para))
        scored.sort(key=lambda s: -s[0])
        limit = int(ctx.config.get("max_results", "4"))
        if not scored:
            return "No matching passages found."
        return "\n\n".join(f"[doc {i}: {title}]\n{para}" for _, i, title, para in scored[:limit])


class ReadDocument(Component):
    name = "read_document"
    summary = "Read a whole company document by its id or exact title (ids come from knowledge_search)."
    input_schema = _schema({"document": {"type": "string", "description": "Document id or title."}})

    def run(self, args, ctx):
        ref = args["document"].strip()
        for doc in ctx.store.knowledge(ctx.agent_id):
            if ref == str(doc["id"]) or ref.lower() == doc["title"].lower():
                return f"[doc {doc['id']}: {doc['title']}]\n{doc['content'][:20000]}"
        raise ToolFailure(f"No document {ref!r}")


# --- structured data ------------------------------------------------------------------

class QueryData(Component):
    name = "query_data"
    summary = ("Run a read-only SQL (SQLite dialect) SELECT query on the company's data. Use filters and "
               "aggregates; results are limited to 200 rows.")
    config_keys = {"datasets": "Comma-separated dataset (table) names this agent may read."}
    required_config = ("datasets",)
    input_schema = _schema({"sql": {"type": "string", "description": "A single SELECT statement."}})

    def describe(self, tool, ctx):
        tables = datasets.describe(ctx.store, ctx.customer_id, _csv(tool.config_dict().get("datasets", "")))
        return f"{self.summary}\nTables:\n{tables}"

    def run(self, args, ctx):
        return datasets.query(ctx.store, ctx.customer_id, _csv(ctx.config.get("datasets", "")), args["sql"])


class SearchRecords(Component):
    name = "search_records"
    summary = "Find existing records (e.g. cases, leads) of this agent's record type by text. Returns id, status and fields."
    config_keys = {"record_type": "Record type to search, e.g. case."}
    required_config = ("record_type",)
    input_schema = _schema({"text": {"type": "string", "description": "Text to match in the record fields."}})

    def run(self, args, ctx):
        rows = ctx.store.search_records(ctx.agent_id, ctx.config["record_type"], args["text"])
        if not rows:
            return "No matching records."
        return "\n".join(f"#{r['id']} [{r['status']}] {r['data_json']}" for r in rows)


class CreateRecord(Component):
    name = "create_record"
    summary = "Create a record (a case, task, lead, flag...) in the company's system. Fields are a JSON object in a string."
    risk = "write_internal"
    config_keys = {
        "record_type": "Record type, e.g. case.",
        "required_fields": "Comma-separated field names every record must have.",
    }
    required_config = ("record_type",)
    input_schema = _schema({"fields_json": {"type": "string", "description": 'JSON object, e.g. {"title": "..."}'}})

    def describe(self, tool, ctx):
        required = tool.config_dict().get("required_fields", "")
        return self.summary + (f" Required fields: {required}." if required else "")

    def run(self, args, ctx):
        data = _parse_json_object(args["fields_json"], "fields_json")
        missing = [f for f in _csv(ctx.config.get("required_fields", "")) if f not in data or data[f] in ("", None)]
        if missing:
            raise ToolFailure(f"missing required fields: {', '.join(missing)}")
        record_id = ctx.store.add_record(ctx.agent_id, ctx.config["record_type"], data)
        return f"Created {ctx.config['record_type']} #{record_id}."


class UpdateRecord(Component):
    name = "update_record"
    summary = "Update fields and/or status of an existing record by id. Changes are a JSON object in a string."
    risk = "write_internal"
    config_keys = {"record_type": "Record type, e.g. case.", "allowed_statuses": "Comma-separated allowed status values."}
    required_config = ("record_type",)
    input_schema = _schema({
        "record_id": {"type": "integer"},
        "changes_json": {"type": "string", "description": "JSON object of fields to change; {} for none."},
        "status": {"type": "string", "description": "New status, or empty string to keep it."},
    })

    def describe(self, tool, ctx):
        allowed = tool.config_dict().get("allowed_statuses", "")
        return self.summary + (f" Allowed statuses: {allowed}." if allowed else "")

    def run(self, args, ctx):
        changes = _parse_json_object(args["changes_json"], "changes_json")
        status = args.get("status") or None
        allowed = _csv(ctx.config.get("allowed_statuses", ""))
        if status and allowed and status not in allowed:
            raise ToolFailure(f"status must be one of: {', '.join(allowed)}")
        row = ctx.store.get_record(ctx.agent_id, int(args["record_id"]))
        if row is None or row["table_name"] != ctx.config["record_type"]:
            raise ToolFailure(f"No {ctx.config['record_type']} #{args['record_id']}")
        ctx.store.update_record(ctx.agent_id, int(args["record_id"]), changes, status)
        return f"Updated {ctx.config['record_type']} #{args['record_id']}."


# --- external systems -----------------------------------------------------------------

class HttpRequest(Component):
    """Declarative connector: one configured endpoint of an external REST API becomes one tool."""

    name = "http_request"
    summary = "Call an external system's API endpoint."
    config_keys = {
        "base_url": "https://host/prefix of the API. Requests can only go to this host.",
        "method": "GET, POST, PUT, PATCH or DELETE.",
        "path": "Path template, e.g. /customers/{customer_id}. Placeholders are filled from the input.",
        "params_schema": "JSON Schema (object) of the tool input, e.g. {\"type\":\"object\",\"properties\":{...}}.",
        "auth_env": "Name of the organisation's secret holding a bearer token, e.g. CRM_TOKEN (uppercase). "
                    "Never the token itself.",
        "effect": "read, write_external or financial. Defaults to read for GET, write_external otherwise.",
        "description": "What the endpoint does and returns.",
        "timeout_seconds": "Request timeout (default 15).",
    }
    required_config = ("base_url", "method", "path")
    strict = False  # the parameter schema is endpoint-specific

    def risk_for(self, tool):
        cfg = tool.config_dict()
        effect = cfg.get("effect") or ("read" if cfg.get("method", "GET").upper() == "GET" else "write_external")
        return effect if effect in ("read", "write_external", "financial") else "write_external"

    def schema_for(self, tool):
        raw = tool.config_dict().get("params_schema", "")
        try:
            schema = json.loads(raw) if raw else {"type": "object", "properties": {}}
        except json.JSONDecodeError:
            schema = {"type": "object", "properties": {}}
        if not isinstance(schema, dict) or schema.get("type") != "object":
            schema = {"type": "object", "properties": {}}
        schema["additionalProperties"] = False
        return schema

    def describe(self, tool, ctx):
        cfg = tool.config_dict()
        return f"{cfg.get('description') or self.summary} ({cfg.get('method', 'GET').upper()} {cfg.get('path', '')})"

    def validate(self, tool):
        problems = super().validate(tool)
        cfg = tool.config_dict()
        url = urllib.parse.urlparse(cfg.get("base_url", ""))
        if cfg.get("base_url") and (url.scheme != "https" or not url.netloc):
            problems.append("base_url must be an https URL")
        if cfg.get("method", "").upper() not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            problems.append("method must be GET, POST, PUT, PATCH or DELETE")
        if cfg.get("auth_env") and not _SECRET_NAME.match(cfg["auth_env"]):
            problems.append("auth_env must be an uppercase secret name like CRM_TOKEN")
        if cfg.get("params_schema"):
            try:
                json.loads(cfg["params_schema"])
            except json.JSONDecodeError:
                problems.append("params_schema is not valid JSON")
        return problems

    def build_request(self, args: dict, cfg: dict, customer_id: int | None = None) -> urllib.request.Request:
        method = cfg["method"].upper()
        args = dict(args)

        def fill(match):
            key = match.group(1)
            if key not in args:
                raise ToolFailure(f"missing path parameter '{key}'")
            return urllib.parse.quote(str(args.pop(key)), safe="")

        path = re.sub(r"\{(\w+)\}", fill, cfg["path"])
        url = cfg["base_url"].rstrip("/") + "/" + path.lstrip("/")
        base = urllib.parse.urlparse(cfg["base_url"])
        if urllib.parse.urlparse(url).netloc != base.netloc:
            raise ToolFailure("request would leave the configured host")
        headers = {"Accept": "application/json", "User-Agent": "agentgen"}
        if cfg.get("auth_env"):
            token = org_secret(customer_id, cfg["auth_env"]) if customer_id is not None else None
            if not token:
                raise ToolFailure(f"credentials not configured (secret {cfg['auth_env']} is not set for this "
                                  "organisation)")
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if method == "GET":
            if args:
                url += "?" + urllib.parse.urlencode({k: v for k, v in args.items() if v not in (None, "")})
        else:
            data = json.dumps(args).encode()
            headers["Content-Type"] = "application/json"
        return urllib.request.Request(url, data=data, headers=headers, method=method)

    def run(self, args, ctx):
        if ctx.sandbox:
            raise ToolFailure("sandbox: this external API was not called (no simulated response is configured); "
                              "sandbox runs and tests never call real systems")
        cfg = ctx.config
        request = self.build_request(args, cfg, ctx.customer_id)
        _check_public_host(urllib.parse.urlparse(request.full_url).hostname or "")
        timeout = float(cfg.get("timeout_seconds", "15"))
        retries = 2 if request.get_method() == "GET" else 0  # only retry idempotent reads
        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=timeout) as resp:
                    body = resp.read(20000).decode("utf-8", "replace")
                    return f"HTTP {resp.status}\n{body[:8000]}"
            except urllib.error.HTTPError as e:
                if e.code >= 500 and attempt < retries:
                    time.sleep(0.5 * 2 ** attempt)
                    continue
                body = e.read(2000).decode("utf-8", "replace")
                raise ToolFailure(f"HTTP {e.code} from API: {body[:1000]}")
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < retries:
                    time.sleep(0.5 * 2 ** attempt)
                    continue
                raise ToolFailure(f"API unreachable: {e}")


# --- communication ---------------------------------------------------------------------

_MESSAGE_SCHEMA = _schema({
    "to": {"type": "string", "description": "Recipient address, channel or phone number."},
    "subject": {"type": "string", "description": "Subject or title; empty if the channel has none."},
    "body": {"type": "string"},
})


class DraftMessage(Component):
    name = "draft_message"
    summary = "Save a message draft for a person to review and send. Sends nothing."
    risk = "write_internal"
    config_keys = {"channel": "email, slack, teams, sms or webhook."}
    input_schema = _MESSAGE_SCHEMA

    def run(self, args, ctx):
        msg_id = ctx.store.add_outbox(ctx.agent_id, "draft", args["to"], args["subject"], args["body"],
                                      ctx.config.get("channel", "email"))
        return f"Draft #{msg_id} saved for review."


class SendMessage(Component):
    name = "send_message"
    summary = "Send a message to someone outside the agent (customer, colleague, supplier)."
    risk = "external_comm"
    config_keys = {
        "channel": "email, slack, teams, sms or webhook.",
        "allowed_recipients": "Comma-separated addresses or @domains the agent may contact. Empty = any.",
    }
    required_config = ("channel",)
    input_schema = _MESSAGE_SCHEMA

    def run(self, args, ctx):
        allowed = [a.lower() for a in _csv(ctx.config.get("allowed_recipients", ""))]
        to = args["to"].strip().lower()
        if allowed and not any(to == a or (a.startswith("@") and to.endswith(a)) for a in allowed):
            raise ToolFailure(f"{args['to']} is not an allowed recipient")
        # Recorded for delivery; a channel connector (Microsoft Graph, Slack, SMS) delivers pending rows.
        msg_id = ctx.store.add_outbox(ctx.agent_id, "pending_delivery", args["to"], args["subject"], args["body"],
                                      ctx.config["channel"])
        return f"Message #{msg_id} queued for delivery via {ctx.config['channel']}."


# --- memory, planning, utilities -------------------------------------------------------

class Remember(Component):
    name = "remember"
    summary = "Store a fact for future runs under a short key (overwrites the same key). Only store what your instructions allow."
    risk = "write_internal"
    input_schema = _schema({"key": {"type": "string"}, "content": {"type": "string"}})

    def run(self, args, ctx):
        if ctx.memory_retention_days <= 0:
            raise ToolFailure("memory is disabled for this agent")
        ctx.store.remember(ctx.agent_id, args["key"][:200], args["content"][:4000], ctx.memory_retention_days)
        return f"Remembered '{args['key']}' for {ctx.memory_retention_days} days."


class Recall(Component):
    name = "recall"
    summary = "Look up facts stored in earlier runs by key or text."
    input_schema = _schema({"query": {"type": "string"}})

    def run(self, args, ctx):
        rows = ctx.store.recall(ctx.agent_id, args["query"])
        return "\n".join(f"{r['key']}: {r['content']} (saved {r['created_at']})" for r in rows) or "Nothing stored."


class ScheduleTask(Component):
    name = "schedule_task"
    summary = "Schedule yourself to run again later with a task description, e.g. to follow up on a case."
    risk = "write_internal"
    config_keys = {"max_delay_minutes": "Longest allowed delay (default 10080 = 7 days)."}
    input_schema = _schema({
        "delay_minutes": {"type": "integer", "description": "Minutes from now."},
        "task": {"type": "string", "description": "What to do then, with the ids and context needed."},
    })

    def run(self, args, ctx):
        delay = int(args["delay_minutes"])
        max_delay = int(ctx.config.get("max_delay_minutes", "10080"))
        if not 1 <= delay <= max_delay:
            raise ToolFailure(f"delay_minutes must be between 1 and {max_delay}")
        job_id = ctx.store.enqueue_job(ctx.agent_id, args["task"], "agent", utcnow() + timedelta(minutes=delay))
        return f"Scheduled job #{job_id} in {delay} minutes."


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod, ast.USub: operator.neg, ast.UAdd: operator.pos}


def _safe_eval(node):
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        right = _safe_eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ToolFailure("exponent too large")
        return _OPS[type(node.op)](_safe_eval(node.left), right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("round", "abs", "min", "max", "sum"):
        values = [_safe_eval(a) for a in node.args]
        return {"round": round, "abs": abs, "min": min, "max": max, "sum": lambda *v: sum(v)}[node.func.id](*values)
    raise ToolFailure("only numbers, + - * / % **, parentheses and round/abs/min/max/sum are allowed")


class Calculate(Component):
    name = "calculate"
    summary = "Evaluate an arithmetic expression exactly (use for totals, VAT, differences, percentages)."
    input_schema = _schema({"expression": {"type": "string", "description": "e.g. round(1450 * 1.25, 2)"}})

    def run(self, args, ctx):
        try:
            tree = ast.parse(args["expression"], mode="eval")
        except SyntaxError:
            raise ToolFailure("not a valid expression")
        try:
            return str(_safe_eval(tree))
        except ZeroDivisionError:
            raise ToolFailure("division by zero")


class EscalateToHuman(Component):
    name = "escalate_to_human"
    summary = ("Hand the task to a person. Use when the request is outside your instructions, information is "
               "missing or contradictory, a tool keeps failing, there is legal/financial risk, or you are unsure.")
    risk = "write_internal"
    input_schema = _schema({
        "reason": {"type": "string", "description": "Why a person is needed."},
        "details": {"type": "string", "description": "Summary of the case and what you already did."},
    })

    def run(self, args, ctx):
        return f"Escalated to a person (#{ctx.store.add_escalation(ctx.agent_id, args['reason'], args['details'])})."


CATALOG: dict[str, Component] = {
    c.name: c
    for c in [
        KnowledgeSearch(), ReadDocument(), QueryData(), SearchRecords(), CreateRecord(), UpdateRecord(),
        HttpRequest(), DraftMessage(), SendMessage(), Remember(), Recall(), ScheduleTask(), Calculate(),
        EscalateToHuman(),
    ]
}

# Risk classes that always need a human to approve each call.
ALWAYS_APPROVE = {"write_external", "external_comm", "financial"}


def risk_of(tool: ToolSpec) -> str:
    return CATALOG[tool.component].risk_for(tool)


def max_risk(tools: list[ToolSpec]) -> str:
    return max((risk_of(t) for t in tools if t.component in CATALOG), key=RISK_ORDER.index, default="read")


def catalog_description() -> str:
    """The component catalog as the designer model sees it."""
    lines = []
    for c in CATALOG.values():
        risk = "depends on config 'effect'" if isinstance(c, HttpRequest) else c.risk
        lines.append(f"- {c.name} [risk: {risk}]: {c.summary}")
        for key, desc in c.config_keys.items():
            req = " (required)" if key in c.required_config else ""
            lines.append(f"    config `{key}`{req}: {desc}")
    return "\n".join(lines)


def is_consequential(tools: list[ToolSpec]) -> bool:
    """Agents that can change business records, act on external systems, communicate
    externally or move money need a recorded human approval before deployment."""
    return any(t.component in ("create_record", "update_record") or risk_of(t) in ALWAYS_APPROVE
               for t in tools if t.component in CATALOG)
