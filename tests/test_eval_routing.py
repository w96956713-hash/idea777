"""Offline evaluation: tenant request classification and routing, and property maintenance handling.

These are deterministic platform checks with a scripted model: a playbook stands in for the model and
decides what to call; the platform's tenant lookup (real SQL over the uploaded CSVs), routing policy
search, case records, approval queue, contractor HTTP connector, outbox, escalations, audit log, worker
and web API are the real code. No network is used: the contractor API is a stub. These tests say
nothing about how well a real model classifies or routes requests.
"""

import io
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from agentgen import components
from agentgen.diagnose import diagnose_run
from agentgen.evaluate import evaluate
from agentgen.providers import ModelUnavailable
from agentgen.runtime import AgentRuntime, approve_all, execute_queued_action, queue_all
from agentgen.web.app import create_app
from agentgen.worker import fire_event, run_due_jobs
from scenario_kit import (PlaybookProvider, events, fail, mark_passed, outbox, property_env, raw_input, records,
                          say, tool, tools)

H = {"X-Requested-With": "agentgen"}
CONTRACTOR_HOST = "api.fastighetsdrift.example"
WORK_ORDER = b'{"work_order_id": "WO-5531", "eta_minutes": 45}'
ORG_TOKEN = "hemvist-fastighetsdrift-token"

TENANT_SQL = ("SELECT t.tenant_id, t.name, t.apartment, t.property_id, p.address, p.caretaker_phone, t.phone, "
              "t.contract_status FROM tenants t JOIN properties p ON p.property_id = t.property_id "
              "WHERE t.apartment = '{apt}'")
REGISTER_APARTMENTS = ("1102", "1203", "0904", "0301", "2201")

LEAK = ("Channel: portal\nFrom: Sara Lindqvist, lgh 1102, Kopparbergsvägen 12\n"
        "Det läcker vatten från taket i badrummet och det rinner ner på golvet!")
LEAK_CASE = {"property_id": "P-01", "apartment": "1102", "tenant_id": "T-1001", "department": "maintenance",
             "priority": "urgent", "summary": "Vattenläcka från taket i badrummet", "channel": "portal"}
DISPATCH = {"address": "Kopparbergsvägen 12", "apartment": "1102", "problem": "Vattenläcka från taket i badrummet",
            "priority": "urgent", "case_id": "1"}
FOLLOWUP = {"delay_minutes": 60, "task": "Check that urgent case #1 (leak, Kopparbergsvägen 12 lgh 1102) has moved."}
SMS = {"to": "0701234501", "subject": "",
       "body": "Hej Sara! Ditt ärende #1 är registrerat och en hantverkare är beställd. /Hemvist"}


def urgent_leak_steps():
    return [
        tool("lookup_tenant", sql=TENANT_SQL.format(apt="1102")),
        tool("create_case", fields_json=json.dumps(LEAK_CASE, ensure_ascii=False)),
        tools(("dispatch_contractor", DISPATCH), ("schedule_followup", FOLLOWUP)),
        tool("notify_tenant", **SMS),
        say("Urgent maintenance case #1 created. The work order and the SMS to the tenant await approval."),
    ]


# ---------------------------------------------------------------- helpers

class FakeContractorAPI:
    """Stands in for the network: records every request urllib would send and the hosts checked."""

    def __init__(self):
        self.requests = []
        self.hosts = []
        self.status, self.body, self.error = 201, WORK_ORDER, None

    def check_host(self, host):
        self.hosts.append(host)

    def urlopen(self, request, timeout):
        self.requests.append({
            "method": request.get_method(), "url": request.full_url,
            "auth": request.get_header("Authorization"), "content_type": request.get_header("Content-type"),
            "body": json.loads(request.data) if request.data else None,
        })
        if self.error is not None:
            raise self.error
        status, body = self.status, self.body

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        response = Response(body)
        response.status = status
        return response


@pytest.fixture
def api(monkeypatch):
    fake = FakeContractorAPI()
    monkeypatch.setattr(components, "http_open", fake.urlopen)
    monkeypatch.setattr(components, "_check_public_host", fake.check_host)
    return fake


def set_org_secret(monkeypatch, cid):
    """The organisation's own token, plus decoys that must never be used: an unscoped variable with the
    spec's secret name and another organisation's scoped secret."""
    monkeypatch.setenv(components.secret_env_name(cid, "FASTIGHETSDRIFT_TOKEN"), ORG_TOKEN)
    monkeypatch.setenv("FASTIGHETSDRIFT_TOKEN", "platform-wide-token-never-used")
    monkeypatch.setenv(components.secret_env_name(cid + 1, "FASTIGHETSDRIFT_TOKEN"), "other-org-token")


def run(store, spec, steps, task, approver=approve_all):
    provider = PlaybookProvider(steps)
    runtime = AgentRuntime(provider, store, store.agent(spec.name)["id"], spec, 1, approver)
    return runtime.run(task), provider


def cases(store):
    return [(r["id"], r["status"], json.loads(r["data_json"])) for r in records(store, "case")]


def escalations(store):
    return [dict(r) for r in store.conn.execute("SELECT * FROM escalations ORDER BY id")]


def actions(store):
    return [dict(r) for r in store.conn.execute("SELECT * FROM pending_actions ORDER BY id")]


def jobs(store):
    return [dict(r) for r in store.conn.execute("SELECT * FROM jobs ORDER BY id")]


def audit_rows(store, event):
    return [json.loads(r["data_json"]) for r in
            store.conn.execute("SELECT data_json FROM audit_log WHERE event = ? ORDER BY id", (event,))]


def live_urgent_run(store, spec):
    """An unattended live run of the urgent-leak request: approvals are queued for a person."""
    return run(store, spec, urgent_leak_steps(), LEAK, approver=queue_all)


# ---------------------------------------------------------------- classification and routing

def test_request_is_matched_to_the_tenant_and_becomes_a_routed_case(tmp_path):
    """lookup_tenant finds the tenant and building in the uploaded register, and create_case stores a case with
    the department, priority and identifiers the agent chose."""
    store, spec, _ = property_env(tmp_path)
    fields = {"property_id": "P-02", "apartment": "0904", "tenant_id": "T-1003", "department": "maintenance",
              "priority": "normal", "summary": "Kranen i köket droppar sedan en vecka", "channel": "email"}
    result, provider = run(store, spec, [
        tool("lookup_tenant", sql=TENANT_SQL.format(apt="0904") + " AND t.email = 'eva.berg@mail.se'"),
        tool("routing_policy", query="dripping tap priority"),
        tool("find_open_cases", text="0904"),
        tool("create_case", fields_json=json.dumps(fields, ensure_ascii=False)),
        say("Created normal-priority maintenance case #1 for Vasagatan 44, lgh 0904."),
    ], "Channel: email\nFrom: eva.berg@mail.se\nHej, kranen i köket droppar lite sedan en vecka. /Eva, lgh 0904")

    lookup, policy, search, created = (c.result for c in result.tool_calls)
    assert lookup.splitlines() == [
        "tenant_id | name | apartment | property_id | address | caretaker_phone | phone | contract_status",
        "T-1003 | Eva Berg | 0904 | P-02 | Vasagatan 44 | 021-400 102 | 0701234503 | active"]
    assert "Priority normal" in policy.split("\n\n")[0] and "dripping taps" in policy.split("\n\n")[0]
    assert search == "No matching records."
    assert created == "Created case #1."
    assert cases(store) == [(1, "open", fields)]
    assert outbox(store) == [] and actions(store) == [] and escalations(store) == []
    assert events(store, result.run_id) == ["run_started", "tool_call", "tool_call", "tool_call", "tool_call",
                                            "run_finished"]
    # The model is shown the agent's own tables and the case's required fields.
    definitions = {t["name"]: t for t in provider.turns[0]["tools"]}
    assert "tenants (5 rows)" in definitions["lookup_tenant"]["description"]
    assert "properties (3 rows)" in definitions["lookup_tenant"]["description"]
    assert "Required fields: property_id,apartment,department,priority,summary" in \
        definitions["create_case"]["description"]


def test_duplicate_report_updates_the_existing_open_case(tmp_path):
    """A second report for the same apartment and problem is merged into the open case with update_case, so
    there is still exactly one case and the original routing fields are kept."""
    store, spec, _ = property_env(tmp_path)
    original = {"property_id": "P-02", "apartment": "0904", "tenant_id": "T-1003", "department": "maintenance",
                "priority": "normal", "summary": "Kranen i köket droppar", "channel": "email"}
    first, _ = run(store, spec, [tool("create_case", fields_json=json.dumps(original, ensure_ascii=False)),
                                 say("Case #1 created.")], "Kranen droppar. /Eva, lgh 0904")
    assert first.tool_calls[0].result == "Created case #1."

    second, _ = run(store, spec, [
        tool("lookup_tenant", sql=TENANT_SQL.format(apt="0904")),
        tool("find_open_cases", text="0904"),
        tool("update_case", record_id=1, changes_json=json.dumps(
            {"update": "Droppar fortfarande, nu mer än förut (portal)."}, ensure_ascii=False), status=""),
        say("Added the new information to open case #1."),
    ], "Channel: portal\nFrom: Eva Berg, 0904\nKranen i köket droppar fortfarande, nu mer än förut.")

    found = second.tool_calls[1].result
    assert found.startswith("#1 [open] ") and '"apartment": "0904"' in found
    assert second.tool_calls[2].result == "Updated case #1."
    assert "create_case" not in second.attempted
    assert cases(store) == [(1, "open", {**original, "update": "Droppar fortfarande, nu mer än förut (portal)."})]
    row = records(store, "case")[0]
    assert row["updated_at"] >= row["created_at"]


def test_report_for_another_apartment_does_not_match_the_open_case(tmp_path):
    """find_open_cases matches on the apartment searched for, so a report from another apartment gets its own
    case instead of being merged into an unrelated one."""
    store, spec, _ = property_env(tmp_path)
    store.add_record(store.agent(spec.name)["id"], "case", {
        "property_id": "P-02", "apartment": "0904", "department": "maintenance", "priority": "normal",
        "summary": "Kranen droppar"})
    result, _ = run(store, spec, [
        tool("find_open_cases", text="1203"),
        tool("create_case", fields_json=json.dumps({
            "property_id": "P-01", "apartment": "1203", "tenant_id": "T-1002", "department": "maintenance",
            "priority": "normal", "summary": "Kranen i badrummet droppar"}, ensure_ascii=False)),
        say("Created case #2."),
    ], "Omar Haddad, 1203: kranen i badrummet droppar.")
    assert result.tool_calls[0].result == "No matching records."
    assert [(i, d["apartment"]) for i, _, d in cases(store)] == [(1, "0904"), (2, "1203")]


def test_unknown_tenant_is_escalated_and_no_apartment_is_guessed(tmp_path):
    """When the register has no match, a case with a blank apartment/property is refused by the required-field
    check, nothing is stored, and the request reaches a person through an escalation."""
    store, spec, _ = property_env(tmp_path)
    result, _ = run(store, spec, [
        tool("lookup_tenant", sql="SELECT tenant_id, name, apartment, property_id FROM tenants WHERE name LIKE 'Kim%'"),
        tool("create_case", fields_json=json.dumps({
            "property_id": "", "apartment": "", "department": "maintenance", "priority": "normal",
            "summary": "Kallt element - hyresgäst ej identifierad (Kim)", "channel": "portal"}, ensure_ascii=False)),
        tool("escalate_to_human", reason="Tenant could not be identified",
             details="'Kim' reports a cold radiator via the portal. No apartment or address given and no match in "
                     "the tenant register. Please ask for apartment number and address."),
        say("I could not identify the tenant, so a person has been asked to get the apartment and address."),
    ], "Channel: portal\nFrom: Kim\nElementet är kallt.")

    lookup, create, escalate = result.tool_calls
    assert lookup.result.splitlines() == ["tenant_id | name | apartment | property_id", "(no rows)"]
    assert create.error and create.result == "Error: missing required fields: property_id, apartment"
    assert escalate.result == "Escalated to a person (#1)."
    assert records(store) == []
    [esc] = escalations(store)
    assert esc["reason"] == "Tenant could not be identified" and "ask for apartment number and address" in esc["details"]
    assert not any(apt in esc["details"] for apt in REGISTER_APARTMENTS)
    assert outbox(store) == [] and actions(store) == []
    assert events(store, result.run_id) == ["run_started", "tool_call", "tool_error", "tool_call", "run_finished"]


def test_rent_dispute_is_routed_to_rent_contracts_and_escalated(tmp_path):
    """A rent dispute becomes a rent_contracts case and an escalation to a person; nothing is sent to the tenant
    and nothing is queued for a contractor."""
    store, spec, _ = property_env(tmp_path)
    fields = {"property_id": "P-02", "apartment": "0301", "tenant_id": "T-1004", "department": "rent_contracts",
              "priority": "normal", "summary": "Bestrider hyreshöjningen och vill ha en avbetalningsplan",
              "channel": "reception notes"}
    result, _ = run(store, spec, [
        tool("lookup_tenant", sql=TENANT_SQL.format(apt="0301") + " AND t.name = 'Jonas Ek'"),
        tool("routing_policy", query="rent disputes payment plans"),
        tool("find_open_cases", text="0301"),
        tool("create_case", fields_json=json.dumps(fields, ensure_ascii=False)),
        tool("escalate_to_human", reason="Rent dispute and payment plan request",
             details="Case #1 (rent_contracts): Jonas Ek, Vasagatan 44 lgh 0301, disputes the rent increase and "
                     "asks for a payment plan. Contract status: notice_given. Not answered."),
        say("Registered case #1 for rent_contracts and handed it to a person. I have not answered the tenant."),
    ], "Channel: reception notes\nJonas Ek, 0301, ringde. Anser att hyreshöjningen är fel och vill ha en "
       "avbetalningsplan.")

    lookup, policy, _, created, escalated = (c.result for c in result.tool_calls)
    assert "T-1004 | Jonas Ek | 0301 | P-02 | Vasagatan 44 | 021-400 102 | 0701234504 | notice_given" in lookup
    assert "go to rent_contracts" in policy.split("\n\n")[0]
    assert created == "Created case #1." and escalated == "Escalated to a person (#1)."
    assert cases(store) == [(1, "open", fields)]
    [esc] = escalations(store)
    assert esc["reason"] == "Rent dispute and payment plan request" and "Case #1 (rent_contracts)" in esc["details"]
    assert outbox(store) == [] and actions(store) == []
    assert result.called == {"lookup_tenant", "routing_policy", "find_open_cases", "create_case", "escalate_to_human"}


# ---------------------------------------------------------------- record validation

def test_create_case_missing_required_fields_fails_clearly_and_creates_nothing(tmp_path):
    """create_case without department and priority returns a tool error naming the missing fields, writes no
    record, is audited as a tool_error and explained by the run diagnosis; the corrected retry creates one case."""
    store, spec, _ = property_env(tmp_path)
    partial = {"property_id": "P-03", "apartment": "2201", "summary": "Trasig lampa i trapphuset"}
    result, _ = run(store, spec, [
        tool("create_case", fields_json=json.dumps(partial, ensure_ascii=False)),
        say("The case could not be created."),
    ], "Lina Persson, 2201: trasig lampa i trapphuset.")

    [call] = result.tool_calls
    assert call.error and call.result == "Error: missing required fields: department, priority"
    assert records(store) == []
    assert events(store, result.run_id) == ["run_started", "tool_error", "run_finished"]
    [error_event] = audit_rows(store, "tool_error")
    assert error_event["tool"] == "create_case" and error_event["result"] == call.result
    finding = next(f for f in diagnose_run(store, result.run_id) if f.title == "Tool create_case returned an error")
    assert "missing required fields: department, priority" in finding.detail
    assert "missing required fields" in finding.suggestion

    retry, _ = run(store, spec, [
        tool("create_case", fields_json=json.dumps(partial, ensure_ascii=False)),
        tool("create_case", fields_json=json.dumps({**partial, "department": "maintenance", "priority": "normal"},
                                                   ensure_ascii=False)),
        say("Created case #1 after correcting the fields."),
    ], "Lina Persson, 2201: trasig lampa i trapphuset.")
    assert [c.error for c in retry.tool_calls] == [True, False]
    assert retry.tool_calls[1].result == "Created case #1."
    assert [d["department"] for _, _, d in cases(store)] == ["maintenance"]


def test_malformed_create_case_inputs_are_rejected_without_writing(tmp_path):
    """Blank or null required values, invalid JSON, a non-object payload and inputs that do not match the tool
    schema are all refused with a specific error, and no record is written."""
    store, spec, _ = property_env(tmp_path)
    base = {"property_id": "P-01", "apartment": "1102", "department": "maintenance", "priority": "urgent",
            "summary": "Läcka"}
    result, _ = run(store, spec, [
        tool("create_case", fields_json=json.dumps({**base, "apartment": None})),
        tool("create_case", fields_json=json.dumps({**base, "priority": "", "summary": ""})),
        tool("create_case", fields_json="property_id=P-01, apartment=1102"),
        tool("create_case", fields_json='["P-01", "1102"]'),
        tool("create_case", fields=base),
        raw_input("create_case", "P-01 1102 maintenance urgent"),
        say("No case created."),
    ], "x")
    messages = [c.result for c in result.tool_calls]
    assert all(c.error for c in result.tool_calls)
    assert messages[0] == "Error: missing required fields: apartment"
    assert messages[1] == "Error: missing required fields: priority, summary"
    assert messages[2].startswith("Error: fields_json is not valid JSON")
    assert messages[3] == "Error: fields_json must be a JSON object"
    assert messages[4] == ("Error: invalid input (missing required field(s): fields_json). Nothing was done; "
                           "correct the input and try again.")
    assert messages[5].startswith("Error: invalid input (the input must be a JSON object)")
    assert records(store) == []


# ---------------------------------------------------------------- urgent repairs: live run, approvals, contractor API

def test_urgent_repair_in_a_live_run_queues_dispatch_and_sms_without_executing(tmp_path, api):
    """In an unattended live run the urgent case and follow-up are created, while dispatch_contractor and
    notify_tenant are only queued for a person: no HTTP request, no outbox row."""
    store, spec, _ = property_env(tmp_path)
    result, _ = live_urgent_run(store, spec)

    assert [(c.name, c.approval) for c in result.tool_calls] == [
        ("lookup_tenant", "not_required"), ("create_case", "not_required"), ("dispatch_contractor", "queued"),
        ("schedule_followup", "not_required"), ("notify_tenant", "queued")]
    assert result.called == {"lookup_tenant", "create_case", "schedule_followup"}
    assert result.tool_calls[2].result.startswith("Submitted for a person's approval (action #1). It has not happened")
    assert result.tool_calls[4].result.startswith("Submitted for a person's approval (action #2).")
    assert cases(store) == [(1, "open", LEAK_CASE)]

    queued = actions(store)
    assert [(a["tool"], a["status"], a["run_id"]) for a in queued] == [
        ("dispatch_contractor", "pending", result.run_id), ("notify_tenant", "pending", result.run_id)]
    assert json.loads(queued[0]["input_json"]) == DISPATCH and json.loads(queued[1]["input_json"]) == SMS
    assert api.requests == [] and api.hosts == []
    assert outbox(store) == []

    [followup] = jobs(store)
    assert followup["source"] == "agent" and followup["status"] == "pending" and followup["input"] == FOLLOWUP["task"]
    delay = datetime.fromisoformat(followup["due_at"]) - datetime.fromisoformat(followup["created_at"])
    assert timedelta(minutes=59) <= delay <= timedelta(minutes=60)

    assert events(store, result.run_id) == ["run_started", "tool_call", "tool_call", "approval", "tool_call",
                                            "approval", "run_finished"]
    assert [(a["tool"], a["decision"]) for a in audit_rows(store, "approval")] == [
        ("dispatch_contractor", "queued"), ("notify_tenant", "queued")]


def test_approved_dispatch_posts_one_work_order_with_the_org_secret(tmp_path, api, monkeypatch):
    """Approving the queued dispatch sends exactly one POST to the configured contractor host, authenticated
    with the organisation-scoped secret and carrying the queued input as JSON; a second approval cannot resend."""
    store, spec, cid = property_env(tmp_path)
    set_org_secret(monkeypatch, cid)
    live_urgent_run(store, spec)
    dispatch, notify = actions(store)

    out = execute_queued_action(store, dispatch["id"], "Anna Karlsson", approve=True)

    assert out == "HTTP 201\n" + WORK_ORDER.decode()
    assert api.requests == [{"method": "POST", "url": f"https://{CONTRACTOR_HOST}/v1/work-orders",
                             "auth": f"Bearer {ORG_TOKEN}", "content_type": "application/json", "body": DISPATCH}]
    assert api.hosts == [CONTRACTOR_HOST]
    decided = store.action(dispatch["id"])
    assert (decided["status"], decided["decided_by"], decided["result"]) == ("approved", "Anna Karlsson", out)
    [approved] = audit_rows(store, "queued_action_approved")
    assert approved == {"action": dispatch["id"], "by": "Anna Karlsson", "result": out}

    with pytest.raises(ValueError, match="already approved"):
        execute_queued_action(store, dispatch["id"], "Bo Svensson", approve=True)
    assert len(api.requests) == 1
    assert store.action(notify["id"])["status"] == "pending" and outbox(store) == []


def test_denied_tenant_sms_leaves_the_outbox_empty(tmp_path, api):
    """Denying the queued notify_tenant records the denial and sends nothing; the action cannot be approved
    afterwards."""
    store, spec, _ = property_env(tmp_path)
    result, _ = live_urgent_run(store, spec)
    _, notify = actions(store)

    assert execute_queued_action(store, notify["id"], "Anna Karlsson", approve=False) == "Denied."
    assert outbox(store) == []
    decided = store.action(notify["id"])
    assert (decided["status"], decided["decided_by"], decided["result"]) == ("denied", "Anna Karlsson", "Denied")
    assert audit_rows(store, "queued_action_denied") == [{"action": notify["id"], "by": "Anna Karlsson"}]
    assert "queued_action_denied" in events(store, result.run_id)

    with pytest.raises(ValueError, match="already denied"):
        execute_queued_action(store, notify["id"], "Bo Svensson", approve=True)
    assert outbox(store) == [] and api.requests == []


def test_approved_dispatch_without_the_org_secret_fails_and_sends_nothing(tmp_path, api, monkeypatch):
    """Without the organisation's own secret the approved dispatch fails with a clear credentials error, the
    unscoped platform variable is not used, and no request leaves the platform."""
    store, spec, cid = property_env(tmp_path)
    monkeypatch.delenv(components.secret_env_name(cid, "FASTIGHETSDRIFT_TOKEN"), raising=False)
    monkeypatch.setenv("FASTIGHETSDRIFT_TOKEN", "platform-wide-token-never-used")
    live_urgent_run(store, spec)
    dispatch, _ = actions(store)

    out = execute_queued_action(store, dispatch["id"], "Anna Karlsson", approve=True)
    assert out == ("Error: credentials not configured (secret FASTIGHETSDRIFT_TOKEN is not set for this "
                   "organisation)")
    assert api.requests == []
    assert store.action(dispatch["id"])["result"] == out


def test_contractor_api_error_on_approval_is_reported_and_not_retried(tmp_path, api, monkeypatch):
    """A 503 from the contractor on the approved POST is returned to the approver as an error after exactly
    one attempt (writes are never retried automatically)."""
    store, spec, cid = property_env(tmp_path)
    set_org_secret(monkeypatch, cid)
    api.error = urllib.error.HTTPError(f"https://{CONTRACTOR_HOST}/v1/work-orders", 503, "Service Unavailable", {},
                                       io.BytesIO(b'{"error": "maintenance window"}'))
    live_urgent_run(store, spec)
    dispatch, _ = actions(store)

    out = execute_queued_action(store, dispatch["id"], "Anna Karlsson", approve=True)
    assert out == 'Error: HTTP 503 from API: {"error": "maintenance window"}'
    assert len(api.requests) == 1 and api.requests[0]["method"] == "POST"
    assert store.action(dispatch["id"])["result"] == out


def test_non_urgent_dispatch_is_rejected_by_the_endpoint_schema_before_approval(tmp_path, api):
    """dispatch_contractor only accepts urgent/high priority: a normal-priority work order is refused as invalid
    input, never reaches the approver and is not queued."""
    store, spec, _ = property_env(tmp_path)
    asked = []
    result, _ = run(store, spec, [
        tool("dispatch_contractor", **{**DISPATCH, "priority": "normal"}),
        tool("dispatch_contractor", **{**DISPATCH, "tenant_phone": "0701234501"}),
        say("Not dispatched."),
    ], "x", approver=lambda name, args: asked.append(name) or "queued")
    assert result.tool_calls[0].result.startswith(
        "Error: invalid input (field 'priority' must be one of ['urgent', 'high'])")
    assert result.tool_calls[1].result.startswith("Error: invalid input (unknown field(s): tenant_phone)")
    assert asked == [] and actions(store) == [] and api.requests == []


# ---------------------------------------------------------------- event-driven live runs

def deploy(store, spec):
    mark_passed(store, spec.name, 1)
    store.approve_version(spec.name, 1, "Karin Holm", "Reviewed routing and approvals.")
    store.deploy(spec.name, 1, requires_approval=True)


def test_new_request_event_runs_the_deployed_router_and_queues_actions(tmp_path, api):
    """A new_request event becomes a job that runs the deployed version with the trigger's input template; its
    dispatch and SMS are queued for approval, not executed."""
    store, spec, _ = property_env(tmp_path)
    deploy(store, spec)
    [job_id] = fire_event(store, spec.name, "new_request", LEAK)
    provider = PlaybookProvider(urgent_leak_steps())

    [outcome] = run_due_jobs(provider, store)

    assert outcome["job"] == job_id and outcome["status"] == "done" and outcome["stop"] == "end_turn"
    assert provider.turns[0]["messages"][0]["content"] == "New tenant request:\n" + LEAK
    run_row = store.conn.execute("SELECT * FROM runs WHERE id = ?", (outcome["run"],)).fetchone()
    assert run_row["trigger_source"] == "event:new_request" and run_row["stop_reason"] == "end_turn"
    assert [(a["tool"], a["status"]) for a in actions(store)] == [("dispatch_contractor", "pending"),
                                                                  ("notify_tenant", "pending")]
    assert len(cases(store)) == 1 and api.requests == [] and outbox(store) == []


def test_retried_request_job_does_not_create_a_second_case_or_dispatch(tmp_path, api):
    """When the model fails mid-run and the job is retried, the case and follow-up already made are not
    repeated and the dispatch is not queued twice: one request stays one case."""
    store, spec, _ = property_env(tmp_path)
    deploy(store, spec)
    [job_id] = fire_event(store, spec.name, "new_request", LEAK)
    steps = urgent_leak_steps()
    provider = PlaybookProvider(steps[:3] + [fail(ModelUnavailable("overloaded"))] + steps)

    [first] = run_due_jobs(provider, store)
    assert first["status"] == "retrying"
    retry_due = datetime.fromisoformat(store.conn.execute("SELECT due_at FROM jobs WHERE id = ?",
                                                          (job_id,)).fetchone()["due_at"])
    [second] = run_due_jobs(provider, store, now=retry_due)

    assert second["status"] == "done"
    assert len(cases(store)) == 1
    assert [a["tool"] for a in actions(store)] == ["dispatch_contractor", "notify_tenant"]
    assert [j["source"] for j in jobs(store)] == ["event:new_request", "agent"]
    assert [e["tool"] for e in audit_rows(store, "tool_repeat_skipped")] == ["create_case", "schedule_followup"]
    assert api.requests == [] and outbox(store) == []


def dispatch_result_in_last_run(provider):
    """What the model was told about dispatch_contractor in the latest run it took part in."""
    messages = provider.turns[-1]["messages"]
    names = {b.id: b.name for m in messages if m["role"] == "assistant" for b in m["content"] if b.type == "tool_use"}
    return next(b["content"] for m in messages if m["role"] == "user" and isinstance(m["content"], list)
                for b in m["content"] if names[b["tool_use_id"]] == "dispatch_contractor")


@pytest.mark.parametrize("decision", ["deny", "approve_but_api_fails"])
def test_retried_job_is_not_told_a_decided_dispatch_still_awaits_approval(tmp_path, api, monkeypatch, decision):
    """If a person decides the queued dispatch before the job is retried (denies it, or approves it and the
    contractor API fails), the retried run is not told the dispatch still awaits approval, so it can escalate."""
    store, spec, cid = property_env(tmp_path)
    set_org_secret(monkeypatch, cid)
    api.error = urllib.error.HTTPError(f"https://{CONTRACTOR_HOST}/v1/work-orders", 503, "Service Unavailable", {},
                                       io.BytesIO(b'{"error": "maintenance window"}'))
    deploy(store, spec)
    [job_id] = fire_event(store, spec.name, "new_request", LEAK)
    steps = urgent_leak_steps()
    provider = PlaybookProvider(steps[:3] + [fail(ModelUnavailable("overloaded"))] + steps)
    [first] = run_due_jobs(provider, store)
    assert first["status"] == "retrying"
    [dispatch] = [a for a in actions(store) if a["tool"] == "dispatch_contractor"]
    execute_queued_action(store, dispatch["id"], "Anna Karlsson", approve=decision != "deny")
    retry_due = datetime.fromisoformat(store.conn.execute("SELECT due_at FROM jobs WHERE id = ?",
                                                          (job_id,)).fetchone()["due_at"])
    [second] = run_due_jobs(provider, store, now=retry_due)
    assert second["status"] == "done"

    told = dispatch_result_in_last_run(provider)
    queued = re.search(r"approval \(action #(\d+)\)", told)
    # Never "Submitted for a person's approval (action #1). It has not happened yet." once #1 is decided.
    assert not (queued and store.action(int(queued.group(1)))["status"] != "pending"), told
    if decision == "deny":
        assert "denied" in told.lower(), told
    else:
        assert queued or "503" in told, told


# ---------------------------------------------------------------- web API

def test_live_run_and_approval_decisions_through_the_web_api(tmp_path, api, monkeypatch):
    """Through the web API a live run of the deployed router queues dispatch and SMS; only an admin of the same
    organisation can decide them; approving posts one work order and denying the SMS leaves the outbox empty."""
    provider = PlaybookProvider(urgent_leak_steps())
    app = create_app(str(tmp_path / "eval.db"), provider_factory=lambda: provider, start_worker=False,
                     inline_operations=True)
    admin = TestClient(app)
    admin.headers.update(H)
    assert admin.post("/api/auth/signup", json={"organization": "Hemvist Fastigheter AB", "name": "Karin",
                                                "email": "karin@hemvist.se", "password": "correct horse battery"}
                      ).status_code == 200
    store, spec, cid = property_env(tmp_path)  # same database and organisation as the signed-up admin
    set_org_secret(monkeypatch, cid)
    mark_passed(store, spec.name, 1)
    store.approve_version(spec.name, 1, "Karin", "Reviewed.")
    agent_id = store.agent(spec.name)["id"]

    assert admin.post(f"/api/agents/{agent_id}/deploy", json={"version": 1}).json() == {"ok": True,
                                                                                       "deployed_version": 1}
    op = admin.post(f"/api/agents/{agent_id}/runs", json={"task": LEAK, "mode": "live"}).json()
    assert op["status"] == "succeeded" and op["result"]["stop_reason"] == "end_turn"
    pending = admin.get("/api/approvals").json()["actions"]
    by_tool = {a["tool"]: a for a in pending}
    assert set(by_tool) == {"dispatch_contractor", "notify_tenant"} and api.requests == []
    run = admin.get(f"/api/runs/{op['result']['run_id']}").json()
    assert [a["tool"] for a in run["actions"]] == ["dispatch_contractor", "notify_tenant"]

    assert admin.post("/api/users", json={"organization": "", "name": "Olle", "email": "olle@hemvist.se",
                                          "password": "another long pass", "role": "member"}).status_code == 200
    member = TestClient(app)
    member.headers.update(H)
    assert member.post("/api/auth/login", json={"email": "olle@hemvist.se",
                                                "password": "another long pass"}).status_code == 200
    outsider = TestClient(app)
    outsider.headers.update(H)
    assert outsider.post("/api/auth/signup", json={"organization": "Annan Förvaltning AB", "name": "Eve",
                                                   "email": "eve@annan.se", "password": "correct horse battery"}
                         ).status_code == 200
    dispatch_id, notify_id = by_tool["dispatch_contractor"]["id"], by_tool["notify_tenant"]["id"]
    assert member.post(f"/api/approvals/actions/{dispatch_id}", json={"decision": "approve"}).status_code == 403
    assert outsider.post(f"/api/approvals/actions/{dispatch_id}", json={"decision": "approve"}).status_code == 404
    assert api.requests == []

    approved = admin.post(f"/api/approvals/actions/{dispatch_id}", json={"decision": "approve"})
    assert approved.status_code == 200 and approved.json()["result"].startswith("HTTP 201")
    assert len(api.requests) == 1 and api.requests[0]["auth"] == f"Bearer {ORG_TOKEN}"
    assert api.requests[0]["body"] == DISPATCH
    assert admin.post(f"/api/approvals/actions/{dispatch_id}", json={"decision": "approve"}).status_code == 400
    denied = admin.post(f"/api/approvals/actions/{notify_id}", json={"decision": "deny"})
    assert denied.status_code == 200 and denied.json() == {"result": "Denied."}
    assert outbox(store) == [] and len(api.requests) == 1
    decided = {a["tool"]: (a["status"], a["decided_by"]) for a in admin.get("/api/approvals").json()["decided"]}
    assert decided == {"dispatch_contractor": ("approved", "Karin"), "notify_tenant": ("denied", "Karin")}


# ---------------------------------------------------------------- evaluation of the routing test cases

def routing_suite(store, spec, cid):
    """Version 2: the spec's own routing cases plus the injected-instruction safety case."""
    names = ["urgent leak", "rent dispute", "unknown tenant", "contractor api down", "injected instruction"]
    v2 = spec.model_copy(deep=True)
    v2.test_cases = [next(c for c in spec.test_cases if c.name == n) for n in names]
    assert store.save_version(cid, v2) == 2
    return v2


def suite_steps(rent_dispute_dispatches=False):
    rent = [tool("lookup_tenant", sql=TENANT_SQL.format(apt="0301")),
            tool("create_case", fields_json=json.dumps({
                "property_id": "P-02", "apartment": "0301", "tenant_id": "T-1004", "department": "rent_contracts",
                "priority": "normal", "summary": "Hyrestvist och avbetalningsplan"}, ensure_ascii=False))]
    if rent_dispute_dispatches:
        rent.append(tool("dispatch_contractor", address="Vasagatan 44", apartment="0301", problem="Rent dispute",
                         priority="high", case_id="1"))
    rent += [tool("escalate_to_human", reason="Rent dispute", details="Case #1 rent_contracts, lgh 0301."),
             say("Case #1 for rent_contracts created and escalated.")]
    return [
        # urgent leak (fixture stands in for the contractor API)
        tool("lookup_tenant", sql=TENANT_SQL.format(apt="1102")),
        tool("create_case", fields_json=json.dumps(LEAK_CASE, ensure_ascii=False)),
        tools(("dispatch_contractor", DISPATCH), ("schedule_followup", FOLLOWUP)),
        tool("notify_tenant", **SMS),
        say("Urgent case #1 created, work order WO-5531 booked, tenant told."),
        # rent dispute
        *rent,
        # unknown tenant
        tool("lookup_tenant", sql="SELECT tenant_id FROM tenants WHERE name LIKE 'Kim%'"),
        tool("escalate_to_human", reason="Tenant could not be identified", details="Kim: cold radiator, no apartment."),
        say("Escalated; asked for apartment and address."),
        # contractor api down (fail_tools)
        tool("lookup_tenant", sql=TENANT_SQL.format(apt="1203")),
        tool("create_case", fields_json=json.dumps({
            "property_id": "P-01", "apartment": "1203", "tenant_id": "T-1002", "department": "maintenance",
            "priority": "urgent", "summary": "Ingen värme, 2 grader ute"}, ensure_ascii=False)),
        tool("dispatch_contractor", address="Kopparbergsvägen 12", apartment="1203", problem="No heating",
             priority="urgent", case_id="1"),
        tool("escalate_to_human", reason="Contractor API down",
             details="Urgent case #1, no heating at lgh 1203. Call caretaker 021-400 101."),
        say("The contractor could not be reached; escalated with caretaker phone 021-400 101."),
        # injected instruction
        tool("create_case", fields_json=json.dumps({
            "property_id": "P-03", "apartment": "2201", "department": "maintenance", "priority": "normal",
            "summary": "Trasig lampa i trapphuset"}, ensure_ascii=False)),
        say("Created a normal case for the stairwell lamp. I ignored the embedded instructions."),
    ]


def test_routing_test_cases_run_in_a_sandbox_and_never_touch_live_data(tmp_path, api):
    """The spec's routing test cases pass with fixtures and simulated outages standing in for the contractor;
    the contractor API is never called and the real store gains no case, message, action or escalation."""
    store, spec, cid = property_env(tmp_path)
    routing_suite(store, spec, cid)
    report = evaluate(PlaybookProvider(suite_steps(), verdict=True), store, spec.name, 2)

    by_name = {c.name: c for c in report.cases}
    assert report.passed, report.blockers()
    assert all(c.tool_check == "ok" for c in report.cases)
    assert "T-1001 | Sara Lindqvist | 1102 | P-01" in by_name["urgent leak"].transcript  # sandbox copy of the data
    assert "[approved]" in by_name["urgent leak"].transcript and "WO-5531" in by_name["urgent leak"].transcript
    assert "Error: service unavailable" in by_name["contractor api down"].transcript
    assert api.requests == [] and api.hosts == []
    assert records(store) == [] and outbox(store) == [] and actions(store) == [] and escalations(store) == []
    assert store.version_status(spec.name, 2) == "passed"


def test_rent_dispute_that_dispatches_a_contractor_fails_its_case(tmp_path, api):
    """Attempting dispatch_contractor on a rent dispute fails that case on the deterministic tool check even
    though the grader passes it, and the version fails the release gate."""
    store, spec, cid = property_env(tmp_path)
    routing_suite(store, spec, cid)
    report = evaluate(PlaybookProvider(suite_steps(rent_dispute_dispatches=True), verdict=True), store, spec.name, 2)

    rent = next(c for c in report.cases if c.name == "rent dispute")
    assert not rent.passed and rent.tool_check == "attempted forbidden dispatch_contractor"
    assert [c.name for c in report.cases if not c.passed] == ["rent dispute"]
    assert not report.passed and "pass rate 80% is below 90%" in report.blockers()
    assert store.version_status(spec.name, 2) == "failed" and api.requests == []
