"""Offline evaluation: missing or contradictory information, invalid datasets and malformed inputs.

These are deterministic platform checks with a scripted model: a PlaybookProvider stands in for the
model and follows a fixed script, while the pipeline, web API, CSV import, tool validation, runtime,
records and audit log are the real code. They prove how the platform handles gaps, conflicts and bad
input; they say nothing about real AI quality.
"""

import io
import json
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from agentgen import components, datasets
from agentgen.diagnose import diagnose_run
from agentgen.evaluate import evaluate
from agentgen.pipeline import Analysis, Discovery, Integration, Opportunity, Question
from agentgen.providers import ModelOutputInvalid
from agentgen.runtime import AgentRuntime, approve_all, deny_all, queue_all
from agentgen.spec import AgentDesign
from agentgen.store import Store
from agentgen.web.app import create_app
from scenario_kit import (PROPERTY, SALES, PlaybookProvider, events, fail, load_spec, outbox, property_env,
                          raw_input, records, say, stop, tool)

H = {"X-Requested-With": "agentgen"}
ORDERS_CSV = (SALES / "data" / "orders.csv").read_text(encoding="utf-8")

INTAKE = {
    "title": "Weekly sales reporting",
    "company": "Nordhem Design",
    "industry": "",
    "problem": "We spend hours every week collecting sales figures from different sources for the weekly report.",
    "workflow": "Every Monday the controller exports orders to Excel and builds the report by hand.",
    "outcome": "A correct weekly report every Monday, emailed to our investors as well as to management.",
    "systems": "E-commerce platform and POS, both export CSV.",
    "constraints": "Reports go to management only. Nothing may leave the company.",
}

Q_RECIPIENTS = "Who may receive the weekly report?"
Q_VAT = "Should revenue include VAT?"
Q_VOLUME = "How many orders do you handle per week?"

DISCOVERY = Discovery(
    missing_information=["Whether revenue figures include or exclude VAT", "Weekly order volume"],
    contradictions=["The outcome has the report emailed to investors, but the constraints say reports go to "
                    "management only and nothing may leave the company."],
    questions=[Question(topic="rules", question=Q_RECIPIENTS, why="Decides whether anything may be sent out."),
               Question(topic="data", question=Q_VAT, why="Changes every total."),
               Question(topic="volumes", question=Q_VOLUME, why="Sizes the agent and its cost.")])

ANALYSIS = Analysis(
    process_summary="The controller compiles weekly sales figures from CSV exports by hand.",
    opportunities=[Opportunity(title="Weekly report", description="Draft the weekly report from order data.",
                               automatable_steps=["query orders", "draft the report"],
                               human_steps=["decide who receives the report"], value="About 3 hours a week",
                               risk="low", recommended=True)],
    recommended_agent="A reporting agent that drafts the weekly report for management.",
    recommended_capabilities=["query sales data", "draft reports"], keep_human=["sending reports"],
    integrations=[Integration(system="POS", purpose="orders", access="read", status="data_import",
                              approach="CSV export")],
    data_needed=["orders"], risks=["sending figures outside the company"], complexity="low",
    complexity_reasons=["one data source"], estimated_runs_per_month=8, open_questions=["Who receives the report?"])


def sales_design() -> AgentDesign:
    return AgentDesign(**load_spec(SALES / "spec.yaml").model_dump(exclude={"model", "effort"}))


# ---------------------------------------------------------------- helpers

def web(tmp_path, provider, org="Nordhem Design") -> TestClient:
    """A signed-in admin of `org` on a fresh server whose model is `provider` (operations run inline)."""
    app = create_app(str(tmp_path / "web.db"), provider_factory=lambda: provider, start_worker=False,
                     inline_operations=True)
    c = TestClient(app, raise_server_exceptions=False)  # server errors show up as 500s, not exceptions
    c.headers.update(H)
    r = c.post("/api/auth/signup", json={"organization": org, "name": "Karin", "email": "karin@example.se",
                                         "password": "correct horse battery"})
    assert r.status_code == 200, r.text
    return c


def ok(r, code=200):
    assert r.status_code == code, r.text
    return r.json()


def upload(c, name, csv):
    return c.post("/api/datasets", json={"name": name, "csv": csv})


def dataset_names(c) -> list[str]:
    return [d["name"] for d in ok(c.get("/api/data"))["datasets"]]


def run(store, spec, steps, task="Handle this request.", approver=deny_all):
    agent_id = store.agent(spec.name)["id"]
    provider = PlaybookProvider(steps)
    return AgentRuntime(provider, store, agent_id, spec, 1, approver).run(task), provider


def escalations(store) -> list[dict]:
    return [dict(r) for r in store.conn.execute("SELECT * FROM escalations ORDER BY id")]


def returned_to_model(provider, turn_index, n=0) -> dict:
    """The n-th tool_result block the model received at the start of turn `turn_index`."""
    return provider.turns[turn_index]["messages"][-1]["content"][n]


def unknown_section(prompt: str) -> list[str]:
    return prompt.split("Unanswered questions (treat as unknown):\n", 1)[1].split("\n\n", 1)[0].splitlines()


# ---------------------------------------------------------------- discovery: missing and contradictory information

def test_discovery_findings_are_stored_and_returned_by_the_api(tmp_path):
    """Discovery's missing_information and contradictions are persisted on the project and returned by GET /api/projects/{id}."""
    provider = PlaybookProvider(outputs={Discovery: DISCOVERY})
    c = web(tmp_path, provider)
    project = ok(c.post("/api/projects", json=INTAKE))
    pid = project["id"]
    assert project["stage"] == "intake" and project["missing_information"] == [] and project["contradictions"] == []

    op = ok(c.post(f"/api/projects/{pid}/questions"))
    assert op["status"] == "succeeded" and op["result"] == {"questions": 3}

    p = ok(c.get(f"/api/projects/{pid}"))
    assert p["stage"] == "discovery"
    assert p["missing_information"] == DISCOVERY.missing_information
    assert p["contradictions"] == DISCOVERY.contradictions
    assert [q["question"] for q in p["questions"]] == [Q_RECIPIENTS, Q_VAT, Q_VOLUME]
    assert p["generated_by"]["questions"] == "playbook (simulated)"
    st = Store(str(tmp_path / "web.db"))
    stored = json.loads(st.project(pid)["questions_json"])
    assert stored["contradictions"] == DISCOVERY.contradictions
    assert stored["missing_information"] == DISCOVERY.missing_information

    # what the model was given: the customer's own (conflicting) statements, and that no data exists yet
    (call,) = provider.structured_calls
    assert call["type"] == "Discovery"
    assert "emailed to our investors" in call["prompt"] and "Reports go to management only" in call["prompt"]
    assert "No datasets or documents have been uploaded to the platform yet." in call["prompt"]
    assert "Industry:" not in call["prompt"]  # a blank optional field is left out, not sent as an empty section


def test_malformed_discovery_output_fails_the_operation_and_stores_nothing(tmp_path):
    """A discovery answer that does not match the expected structure fails the operation with a retryable message and leaves the project unchanged."""
    bad = ModelOutputInvalid("The model's answer did not match the expected structure.")
    c = web(tmp_path, PlaybookProvider(outputs={Discovery: bad}))
    pid = ok(c.post("/api/projects", json=INTAKE))["id"]
    op = ok(c.post(f"/api/projects/{pid}/questions"))
    assert op["status"] == "failed"
    assert op["error"].startswith("The model's answer did not match the expected structure. Try again.")
    assert op["result"]["error_kind"] == "ModelOutputInvalid" and op["result"]["retryable"] is True
    p = ok(c.get(f"/api/projects/{pid}"))
    assert p["stage"] == "intake" and p["questions"] == []
    assert p["missing_information"] == [] and p["contradictions"] == []
    assert p["operations"][0]["status"] == "failed"


def test_blank_answers_reach_analysis_and_design_as_unknown(tmp_path):
    """Blank, whitespace-only and never-given answers are passed to analysis and design as 'unknown', never as answers."""
    provider = PlaybookProvider(outputs={Discovery: DISCOVERY, Analysis: ANALYSIS, AgentDesign: sales_design()})
    c = web(tmp_path, provider)
    pid = ok(c.post("/api/projects", json=INTAKE))["id"]
    ok(c.post(f"/api/projects/{pid}/questions"))
    p = ok(c.put(f"/api/projects/{pid}/answers", json={"answers": [
        {"question": Q_RECIPIENTS, "answer": "Management only: the CEO and the controller."},
        {"question": Q_VAT, "answer": "   "},           # whitespace only
        {"question": "Any other systems we should know about?"},  # answer left out entirely
    ]}))  # Q_VOLUME is not answered at all
    assert [a["answer"] for a in p["answers"]] == ["Management only: the CEO and the controller.", "", ""]

    assert ok(c.post(f"/api/projects/{pid}/analysis"))["status"] == "succeeded"
    analysis_call = provider.structured_calls[-1]
    assert analysis_call["type"] == "Analysis"
    prompt = analysis_call["prompt"]
    assert f"Q: {Q_RECIPIENTS}\nA: Management only: the CEO and the controller." in prompt
    assert f"Q: {Q_VAT}" not in prompt and "Q: Any other systems" not in prompt  # blanks are not answers
    unknown = [f"- {Q_VAT}", f"- {Q_VOLUME}", "- Any other systems we should know about?"]
    assert unknown_section(prompt) == unknown

    ok(c.put(f"/api/projects/{pid}/amendments", json={"amendments": "Reports go to management only; never to investors."}))
    design_op = ok(c.post(f"/api/projects/{pid}/design"))
    assert design_op["status"] == "succeeded", design_op
    design_call = provider.structured_calls[-1]
    assert design_call["type"] == "AgentDesign"
    assert unknown_section(design_call["prompt"]) == unknown
    assert ("Corrections and decisions from the customer's review (these override anything above):\n"
            "Reports go to management only; never to investors.") in design_call["prompt"]


def test_unresolved_discovery_findings_reach_the_analysis(tmp_path):
    """Contradictions and missing information that discovery found, and nobody resolved, are flagged to the analysis stage."""
    discovery = DISCOVERY.model_copy(update={"questions": [
        Question(topic="process", question="Which weekday is the report due?", why="Sets the schedule.")]})
    provider = PlaybookProvider(outputs={Discovery: discovery, Analysis: ANALYSIS})
    c = web(tmp_path, provider)
    pid = ok(c.post("/api/projects", json=INTAKE))["id"]
    ok(c.post(f"/api/projects/{pid}/questions"))
    assert ok(c.post(f"/api/projects/{pid}/analysis"))["status"] == "succeeded"
    prompt = provider.structured_calls[-1]["prompt"]
    for finding in discovery.contradictions + discovery.missing_information:
        assert finding in prompt, finding


def test_incomplete_or_malformed_requirements_are_rejected(tmp_path):
    """An intake without the problem, or malformed answers, is rejected with 422 naming the field, and nothing is stored."""
    c = web(tmp_path, PlaybookProvider())
    r = c.post("/api/projects", json={k: v for k, v in INTAKE.items() if k != "problem"})
    assert r.status_code == 422 and ["body", "problem"] in [e["loc"] for e in r.json()["detail"]]
    r = c.post("/api/projects", json={**INTAKE, "problem": "Reports."})
    assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["body", "problem"]
    assert c.post("/api/projects", json={**INTAKE, "company": ""}).status_code == 422
    assert ok(c.get("/api/projects")) == []

    pid = ok(c.post("/api/projects", json=INTAKE))["id"]
    for bad in ({"answers": "VAT is excluded"}, {"answers": [{"answer": "no question given"}]},
                {"answers": [{"question": "q", "answer": "a"}] * 41}, {"answers": [{"question": "q", "answer": 7}]}):
        r = c.put(f"/api/projects/{pid}/answers", json=bad)
        assert r.status_code == 422, bad
    assert ok(c.get(f"/api/projects/{pid}"))["answers"] == []


# ---------------------------------------------------------------- agents facing missing or contradictory information

def test_unidentifiable_request_is_escalated_instead_of_acted_on(tmp_path):
    """With no way to identify the tenant, the agent escalates: an escalation row exists and no case, message or action does."""
    store, spec, _ = property_env(tmp_path)
    result, provider = run(store, spec, [
        tool("lookup_tenant", sql="SELECT tenant_id, name, apartment FROM tenants WHERE name LIKE '%Kim%'"),
        tool("escalate_to_human", reason="Cannot identify tenant or building",
             details="Portal request from 'Kim': the radiator is cold. No apartment or address given and no tenant "
                     "named Kim in the register."),
        say("I could not identify the tenant or the building, so a person will follow up."),
    ], task="Channel: portal\nFrom: Kim\nElementet är kallt.")
    lookup, escalation = result.tool_calls
    assert lookup.result == "tenant_id | name | apartment\n(no rows)" and not lookup.error
    assert escalation.approval == "not_required" and escalation.result == "Escalated to a person (#1)."
    (row,) = escalations(store)
    assert row["reason"] == "Cannot identify tenant or building" and "no tenant named Kim" in row["details"]
    assert row["agent_id"] == store.agent(spec.name)["id"]
    assert records(store) == [] and outbox(store) == [] and store.pending_actions() == []
    assert events(store, result.run_id) == ["run_started", "tool_call", "tool_call", "run_finished"]
    # the platform tells the model to escalate rather than guess, and offers the tool for exactly that
    assert "If information is missing or contradictory, say so and ask for it or escalate; do not guess." \
        in provider.turns[0]["system"]
    escalate_def = next(t for t in provider.turns[0]["tools"] if t["name"] == "escalate_to_human")
    assert "information is missing or contradictory" in escalate_def["description"]


def test_contradictory_tenant_details_are_escalated_with_the_conflicting_records(tmp_path):
    """When the name and apartment point to different tenants, the real register shows the conflict and the agent escalates without creating a case."""
    store, spec, _ = property_env(tmp_path)
    result, _ = run(store, spec, [
        tool("lookup_tenant", sql="SELECT tenant_id, name, apartment FROM tenants "
                                  "WHERE name = 'Sara Lindqvist' OR apartment = '1203' ORDER BY tenant_id"),
        tool("escalate_to_human", reason="Name and apartment do not match",
             details="Sender says Sara Lindqvist, lgh 1203. Register: Sara Lindqvist is T-1001 in 1102; 1203 is "
                     "T-1002 Omar Haddad. Which apartment is leaking?"),
        say("The name and apartment do not match our register; a person will confirm before a case is opened."),
    ], task="Channel: portal\nFrom: Sara Lindqvist, lgh 1203, Kopparbergsvägen 12\nDet läcker från diskmaskinen.")
    lookup = result.tool_calls[0].result.splitlines()
    assert lookup == ["tenant_id | name | apartment", "T-1001 | Sara Lindqvist | 1102", "T-1002 | Omar Haddad | 1203"]
    (row,) = escalations(store)
    assert row["reason"] == "Name and apartment do not match" and "T-1001" in row["details"] and "T-1002" in row["details"]
    assert result.attempted == {"lookup_tenant", "escalate_to_human"}
    assert records(store) == [] and store.pending_actions() == []


def test_required_case_fields_cannot_be_left_empty(tmp_path):
    """create_record refuses a case whose required fields are empty, null or absent; nothing is stored and the model gets the error."""
    store, spec, _ = property_env(tmp_path)
    result, provider = run(store, spec, [
        tool("create_case", fields_json=json.dumps({"property_id": "P-01", "apartment": "", "department": "maintenance",
                                                    "priority": None, "summary": "Radiator cold"})),
        tool("create_case", fields_json=json.dumps({"summary": "Radiator cold"})),
        tool("escalate_to_human", reason="Missing apartment", details="Cannot register the case without an apartment."),
        say("Escalated: the apartment is missing."),
    ], approver=approve_all)
    first, second, _ = result.tool_calls
    assert first.error and first.result == "Error: missing required fields: apartment, priority"
    assert second.error and second.result == "Error: missing required fields: property_id, apartment, department, priority"
    assert records(store) == []
    assert len(escalations(store)) == 1
    assert events(store, result.run_id) == ["run_started", "tool_error", "tool_error", "tool_call", "run_finished"]
    assert returned_to_model(provider, 1) == {"type": "tool_result", "tool_use_id": "toolu_1", "content": first.result,
                                              "is_error": True}


def test_whitespace_only_required_fields_count_as_missing(tmp_path):
    """A required case field that holds only whitespace is missing information and the case is not created."""
    store, spec, _ = property_env(tmp_path)
    result, _ = run(store, spec, [
        tool("create_case", fields_json=json.dumps({"property_id": "P-01", "apartment": "   ", "department": "maintenance",
                                                    "priority": "normal", "summary": " \t"})),
        say("done"),
    ], approver=approve_all)
    call = result.tool_calls[0]
    assert call.error and call.result == "Error: missing required fields: apartment, summary"
    assert records(store) == []


def test_guessing_on_missing_information_fails_the_missing_info_test(tmp_path):
    """The release gate fails a version that guesses and dispatches on missing information, and passes one that escalates."""
    store, spec, cid = property_env(tmp_path)
    spec = spec.model_copy(deep=True)
    spec.test_cases = [c for c in spec.test_cases if c.name in ("unknown tenant", "disturbance identity")]
    assert [c.category for c in spec.test_cases] == ["missing_info", "permission"]
    store.save_version(cid, spec)  # v2
    lookup = tool("lookup_tenant", sql="SELECT tenant_id FROM tenants WHERE name LIKE '%Kim%'")

    careful = PlaybookProvider([
        lookup, tool("escalate_to_human", reason="Unknown tenant", details="Need apartment and address from 'Kim'."),
        say("I need the apartment and address; a person will contact Kim."),
        say("I can't tell you who complained."),
    ])
    report = evaluate(careful, store, spec.name, 2)
    assert report.passed and [c.tool_check for c in report.cases] == ["ok", "ok"]

    guesser = PlaybookProvider([
        lookup,
        tool("create_case", fields_json=json.dumps({"property_id": "P-01", "apartment": "1102", "department": "maintenance",
                                                    "priority": "urgent", "summary": "Cold radiator"})),
        tool("dispatch_contractor", address="Kopparbergsvägen 12", apartment="1102", problem="Cold radiator",
             priority="urgent", case_id="1"),
        say("A contractor is on the way to apartment 1102."),
        say("I can't tell you who complained."),
    ])
    report = evaluate(guesser, store, spec.name, 2)  # the grader says pass; the deterministic check does not
    missing = report.cases[0]
    assert missing.name == "unknown tenant" and not missing.passed
    assert missing.tool_check == "attempted forbidden dispatch_contractor"
    assert not report.passed and report.pass_rate == 0.5
    assert "pass rate 50% is below 90%" in report.blockers()
    assert records(store) == [] and escalations(store) == []  # tests ran in a sandbox copy


def test_agent_without_its_data_gets_a_clear_error_and_escalates(tmp_path):
    """When the customer's dataset was never uploaded, the tool says so (description and error), it is audited, and the agent escalates instead of drafting a report."""
    store = Store(str(tmp_path / "eval.db"))
    cid = store.get_or_create_customer("Nordhem Design")
    spec = load_spec(SALES / "spec.yaml")
    store.save_version(cid, spec, origin="imported")
    result, provider = run(store, spec, [
        tool("sales_data", sql="SELECT SUM(quantity * unit_price_sek) FROM orders"),
        tool("escalate_to_human", reason="Sales data missing", details="No order data has been uploaded."),
        say("I can't prepare the report: no sales data has been uploaded. A person has been notified."),
    ], task="Prepare the weekly sales report.")
    sales_def = next(t for t in provider.turns[0]["tools"] if t["name"] == "sales_data")
    assert "- (no datasets imported yet)" in sales_def["description"]
    query = result.tool_calls[0]
    assert query.error and query.result == "Error: no datasets have been imported for this customer."
    assert escalations(store)[0]["reason"] == "Sales data missing"
    assert outbox(store) == []  # no report was drafted from nothing
    evs = store.run_events(result.run_id)
    assert [e["event"] for e in evs] == ["run_started", "tool_error", "tool_call", "run_finished"]
    assert json.loads(evs[1]["data_json"])["result"] == query.result


# ---------------------------------------------------------------- malformed tool input from the model

ORIGINAL_CASE = {"property_id": "P-02", "apartment": "0904", "department": "maintenance", "priority": "normal",
                 "summary": "Dripping kitchen tap"}

MALFORMED = [
    pytest.param(tool("create_case"), "missing required field(s): fields_json", id="missing-required-field"),
    pytest.param(tool("update_case", record_id="1", changes_json="{}", status="closed"),
                 "field 'record_id' must be of type integer", id="string-for-integer"),
    pytest.param(tool("update_case", record_id=True, changes_json="{}", status="closed"),
                 "field 'record_id' must be of type integer", id="boolean-for-integer"),
    pytest.param(tool("update_case", record_id=1, changes_json={"note": "worse"}, status="closed"),
                 "field 'changes_json' must be of type string", id="object-for-string"),
    pytest.param(tool("schedule_followup", delay_minutes="60", task="Check case #1"),
                 "field 'delay_minutes' must be of type integer", id="string-for-delay"),
    pytest.param(tool("create_case", fields_json="{}", priority="urgent"), "unknown field(s): priority",
                 id="unknown-extra-field"),
    pytest.param(raw_input("create_case", '{"fields_json": "{}"}'), "the input must be a JSON object",
                 id="json-text-instead-of-object"),
    pytest.param(raw_input("update_case", [1, "{}", "closed"]), "the input must be a JSON object", id="list-input"),
    pytest.param(raw_input("create_case", None), "the input must be a JSON object", id="null-input"),
]


@pytest.mark.parametrize("step, problem", MALFORMED)
def test_malformed_tool_input_never_executes(tmp_path, step, problem):
    """A malformed tool input is rejected before the tool runs, returned to the model as an error result, and audited as tool_error."""
    store, spec, _ = property_env(tmp_path)
    store.add_record(store.agent(spec.name)["id"], "case", ORIGINAL_CASE)  # case #1, which a bad update must not touch
    result, provider = run(store, spec, [step, say("I could not complete that.")], approver=approve_all)

    (call,) = result.tool_calls
    expected = f"Error: invalid input ({problem}). Nothing was done; correct the input and try again."
    assert call.error and call.result == expected and call.approval == "not_required"
    if not isinstance(step[2], dict):
        assert call.input == {"raw": str(step[2])}
    # nothing executed: the existing case is unchanged and no record or follow-up job was created
    (case,) = records(store)
    assert case["status"] == "open" and json.loads(case["data_json"]) == ORIGINAL_CASE
    assert store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    # the model is told, as an error result for that tool call
    assert returned_to_model(provider, 1) == {"type": "tool_result", "tool_use_id": "toolu_1", "content": expected,
                                              "is_error": True}
    evs = store.run_events(result.run_id)
    assert [e["event"] for e in evs] == ["run_started", "tool_error", "run_finished"]
    data = json.loads(evs[1]["data_json"])
    assert data == {"tool": step[1], "input": step[2], "result": f"Invalid input: {problem}", "invalid_input": True}
    assert result.stop_reason == "end_turn"


@pytest.mark.parametrize("fields_json, message", [
    ("{property_id: P-01, apartment: 1102}", "fields_json is not valid JSON"),
    ("", "fields_json is not valid JSON"),
    ('{"summary": "leak"', "fields_json is not valid JSON"),
    ('["P-01", "1102", "maintenance"]', "fields_json must be a JSON object"),
    ('"water leak in 1102"', "fields_json must be a JSON object"),
    ("null", "fields_json must be a JSON object"),
    ("42", "fields_json must be a JSON object"),
])
def test_fields_json_that_is_not_a_json_object_creates_nothing(tmp_path, fields_json, message):
    """create_record with fields_json that is not valid JSON, or not a JSON object, creates no record and reports a tool_error."""
    store, spec, _ = property_env(tmp_path)
    result, provider = run(store, spec, [tool("create_case", fields_json=fields_json), say("Could not create it.")],
                           approver=approve_all)
    (call,) = result.tool_calls
    assert call.error and call.result.startswith(f"Error: {message}")
    assert records(store) == []
    assert returned_to_model(provider, 1)["is_error"] is True
    assert returned_to_model(provider, 1)["content"] == call.result
    evs = store.run_events(result.run_id)
    assert [e["event"] for e in evs] == ["run_started", "tool_error", "run_finished"]
    assert json.loads(evs[1]["data_json"])["result"] == call.result


def test_invalid_changes_json_leaves_the_record_untouched(tmp_path):
    """update_record with changes_json that is invalid or not an object changes neither the fields nor the status."""
    store, spec, _ = property_env(tmp_path)
    rid = store.add_record(store.agent(spec.name)["id"], "case", ORIGINAL_CASE)
    result, _ = run(store, spec, [
        tool("update_case", record_id=rid, changes_json="{'note': 'worse now'}", status="closed"),
        tool("update_case", record_id=rid, changes_json='["worse now"]', status="closed"),
        say("Could not update the case."),
    ], approver=approve_all)
    first, second = result.tool_calls
    assert first.error and first.result.startswith("Error: changes_json is not valid JSON")
    assert second.error and second.result == "Error: changes_json must be a JSON object"
    (case,) = records(store)
    assert case["status"] == "open" and json.loads(case["data_json"]) == ORIGINAL_CASE
    assert events(store, result.run_id) == ["run_started", "tool_error", "tool_error", "run_finished"]


def test_model_can_correct_an_invalid_input_and_the_tool_then_runs_once(tmp_path):
    """After an invalid-input error the model's corrected call executes exactly once."""
    store, spec, _ = property_env(tmp_path)
    good = json.dumps({"property_id": "P-03", "apartment": "2201", "department": "keys_locks", "priority": "normal",
                       "summary": "Laundry key card stopped working"})
    result, provider = run(store, spec, [
        tool("create_case", fields_json=good, tenant="T-1005"),
        tool("create_case", fields_json=good),
        say("Case #1 created."),
    ], approver=approve_all)
    bad, fixed = result.tool_calls
    assert bad.result.startswith("Error: invalid input (unknown field(s): tenant)")
    assert fixed.result == "Created case #1." and not fixed.error
    (case,) = records(store, "case")
    assert json.loads(case["data_json"])["apartment"] == "2201"
    assert events(store, result.run_id) == ["run_started", "tool_error", "tool_call", "run_finished"]
    assert returned_to_model(provider, 2) == {"type": "tool_result", "tool_use_id": "toolu_2", "content": "Created case #1."}


def test_malformed_input_for_an_external_api_never_reaches_the_approver_or_the_network(tmp_path, monkeypatch):
    """Malformed inputs to an approval-gated HTTP tool are rejected before approval, queuing or any request; a valid one goes through once."""
    store, spec, cid = property_env(tmp_path)
    monkeypatch.setenv(components.secret_env_name(cid, "FASTIGHETSDRIFT_TOKEN"), "test-token")
    monkeypatch.setattr(components, "_check_public_host", lambda host: None)
    sent = []

    class Resp(io.BytesIO):
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout):
        sent.append({"url": request.full_url, "body": json.loads(request.data)})
        return Resp(b'{"work_order_id": "WO-9", "eta_minutes": 40}')

    monkeypatch.setattr(components, "http_open", fake_urlopen)
    asked = []

    def approver(name, args):
        asked.append(name)
        return True

    good = {"address": "Kopparbergsvägen 12", "apartment": "1102", "problem": "Water leak", "priority": "urgent",
            "case_id": "7"}
    result, _ = run(store, spec, [
        tool("dispatch_contractor", **{k: v for k, v in good.items() if k != "case_id"}),
        tool("dispatch_contractor", **{**good, "priority": "low"}),
        tool("dispatch_contractor", **{**good, "callback_url": "https://attacker.example/hook"}),
        tool("dispatch_contractor", **{**good, "apartment": 1102}),
        raw_input("dispatch_contractor", json.dumps(good)),
        tool("dispatch_contractor", **good),
        say("Work order WO-9 requested."),
    ], approver=approver)
    problems = [c.result for c in result.tool_calls[:5]]
    assert all(p.startswith("Error: invalid input (") for p in problems)
    assert "missing required field(s): case_id" in problems[0]
    assert "field 'priority' must be one of ['urgent', 'high']" in problems[1]
    assert "unknown field(s): callback_url" in problems[2]
    assert "field 'apartment' must be of type string" in problems[3]
    assert "the input must be a JSON object" in problems[4]
    assert asked == ["dispatch_contractor"]  # only the valid call was put to the approver
    assert sent == [{"url": "https://api.fastighetsdrift.example/v1/work-orders", "body": good}]
    assert result.tool_calls[5].result.startswith("HTTP 201") and result.tool_calls[5].approval == "approved"
    assert events(store, result.run_id) == ["run_started"] + ["tool_error"] * 5 + ["approval", "tool_call",
                                                                                  "run_finished"]

    # unattended (queue_all): a malformed action is never queued for a person to approve
    result, _ = run(store, spec, [tool("dispatch_contractor", **{**good, "priority": "low"}), say("Could not dispatch.")],
                    approver=queue_all)
    assert result.tool_calls[0].approval == "not_required" and result.tool_calls[0].error
    assert store.pending_actions() == [] and len(sent) == 1


def test_malformed_input_is_visible_in_run_details_and_diagnosis(tmp_path):
    """Through the web API, a non-object tool input appears as a tool_error event in the run and the diagnosis names it as malformed input."""
    provider = PlaybookProvider([raw_input("create_case", "apartment 1102: water leak"),
                                 say("I could not register the case.")])
    c = web(tmp_path, provider, org="Hemvist Fastigheter AB")
    st = Store(str(tmp_path / "web.db"))
    cid = st.get_or_create_customer("Hemvist Fastigheter AB")
    datasets.import_csv(st, cid, "tenants", PROPERTY / "data" / "tenants.csv")
    datasets.import_csv(st, cid, "properties", PROPERTY / "data" / "properties.csv")
    st.save_version(cid, load_spec(PROPERTY / "spec.yaml"), origin="imported")
    st.close()
    agent_id = ok(c.get("/api/agents"))[0]["id"]

    op = ok(c.post(f"/api/agents/{agent_id}/runs", json={"task": "Water leak in 1102", "mode": "sandbox"}))
    assert op["status"] == "succeeded", op
    run_detail = ok(c.get(f"/api/runs/{op['result']['run_id']}"))
    assert run_detail["sandbox"] and run_detail["status"] == "completed"
    errors = [e["data"] for e in run_detail["events"] if e["event"] == "tool_error"]
    assert errors == [{"tool": "create_case", "input": "apartment 1102: water leak",
                       "result": "Invalid input: the input must be a JSON object", "invalid_input": True}]
    assert not any(e["event"] == "tool_call" for e in run_detail["events"])
    findings = ok(c.get(f"/api/runs/{op['result']['run_id']}/diagnosis"))
    finding = next(f for f in findings if f["title"] == "Tool create_case returned an error")
    assert finding["severity"] == "warning" and "malformed tool input" in finding["suggestion"]


def test_tool_use_turn_without_a_tool_call_ends_the_run_as_an_error(tmp_path):
    """A turn that stops for tool_use but carries no tool call ends the run as an error with a clear message and runs nothing."""
    store, spec, _ = property_env(tmp_path)
    result, _ = run(store, spec, [stop("tool_use", "Let me create the case.")], approver=approve_all)
    assert result.stop_reason == "error" and result.text == "The model asked to use a tool but sent no tool call."
    assert result.tool_calls == [] and records(store) == []
    row = store.conn.execute("SELECT stop_reason, output FROM runs WHERE id = ?", (result.run_id,)).fetchone()
    assert tuple(row) == ("error", "The model asked to use a tool but sent no tool call.")
    assert diagnose_run(store, result.run_id)[0].title == "The run failed"


def test_unreadable_model_output_is_retried_once_then_fails_the_run(tmp_path):
    """One unreadable model answer is retried without side effects; a second one fails the run with a clear, audited error."""
    store, spec, _ = property_env(tmp_path)
    unreadable = ModelOutputInvalid("The model's answer could not be read (JSONDecodeError).")
    result, provider = run(store, spec, [fail(unreadable), tool("lookup_tenant", sql="SELECT COUNT(*) FROM tenants"),
                                         say("There are 5 tenants.")])
    assert result.stop_reason == "end_turn" and len(provider.turns) == 3
    assert result.tool_calls[0].result.splitlines()[1] == "5"

    with pytest.raises(ModelOutputInvalid):
        run(store, spec, [fail(unreadable), fail(unreadable), tool("lookup_tenant", sql="SELECT 1")])
    failed = store.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    assert failed["stop_reason"] == "error" and "could not be read" in failed["error"] and "Try again" in failed["error"]
    assert events(store, failed["id"]) == ["run_started", "run_failed"]


# ---------------------------------------------------------------- CSV uploads: empty, header-only, named wrong, too big, ragged

def test_empty_csv_uploads_are_rejected(tmp_path):
    """Empty, blank-line and BOM-only CSV uploads are rejected with 422 and a clear message, and no dataset is registered."""
    c = web(tmp_path, PlaybookProvider())
    r = upload(c, "orders", "")
    assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["body", "csv"]
    for blank in ("\n\n   \n", "﻿", "﻿\r\n\t\r\n"):
        r = upload(c, "orders", blank)
        assert r.status_code == 422 and r.json()["detail"] == "The CSV file is empty", repr(blank)
    assert dataset_names(c) == []


def test_header_only_csv_is_rejected(tmp_path):
    """A CSV with a header but no data rows (blank or empty-cell lines don't count) is rejected with a clear message."""
    c = web(tmp_path, PlaybookProvider())
    for text in ("order_id,amount_sek\n", "order_id;amount_sek\r\n;\r\n", "﻿order_id,amount_sek\n\n , \n"):
        r = upload(c, "orders", text)
        assert r.status_code == 422, text
        assert r.json()["detail"] == "The CSV needs a header row and at least one data row"
    assert dataset_names(c) == []


@pytest.mark.parametrize("name", ["Orders", "1orders", "orders-2026", "orders.csv", "_orders", "", "ordrar_å",
                                  "orders ", "a" * 64])
def test_wrongly_named_datasets_are_refused(tmp_path, name):
    """Dataset names outside lowercase letters, digits and underscores (starting with a letter, max 63) are refused with 422 on the name field."""
    c = web(tmp_path, PlaybookProvider())
    r = upload(c, name, ORDERS_CSV)
    assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["body", "name"]
    assert dataset_names(c) == []
    with pytest.raises(ValueError, match="lowercase letters, digits and underscores"):
        datasets.import_csv_text(Store(str(tmp_path / "web.db")), 1, name, ORDERS_CSV)


def test_longest_allowed_dataset_name_is_accepted(tmp_path):
    """A 63-character dataset name, the documented maximum, is accepted."""
    c = web(tmp_path, PlaybookProvider())
    assert ok(upload(c, "a" * 63, ORDERS_CSV))["rows"] == 12


@pytest.mark.parametrize("name", ["sqlite_master", "sqlite_sequence", "sqlite_orders"])
def test_reserved_sqlite_dataset_names_are_refused_cleanly(tmp_path, name):
    """A dataset name SQLite reserves for itself is refused with a 422 message instead of a server error."""
    c = web(tmp_path, PlaybookProvider())
    ok(upload(c, "orders", ORDERS_CSV))
    r = upload(c, name, "a,b\n1,2\n")
    assert r.status_code == 422, (r.status_code, r.text)
    assert "name" in r.json()["detail"].lower()
    assert dataset_names(c) == ["orders"] and len(ok(c.get("/api/datasets/orders"))["rows"]) == 12


INJECTED_NAMES = ['orders"; DROP TABLE orders; --', "orders; DROP TABLE orders", "x' OR '1'='1",
                  'orders" (order_id) VALUES (1); --', "orders--", 'orders" UNION SELECT * FROM products --']


def test_sql_injection_in_dataset_names_is_refused(tmp_path):
    """Dataset names that try SQL injection are refused on upload, preview and delete, and the existing data stays intact."""
    c = web(tmp_path, PlaybookProvider())
    ok(upload(c, "orders", ORDERS_CSV))
    st = Store(str(tmp_path / "web.db"))
    cid = st.get_or_create_customer("Nordhem Design")
    for name in INJECTED_NAMES:
        r = upload(c, name, "order_id\n1\n")
        assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["body", "name"], name
        path = "/api/datasets/" + urllib.parse.quote(name, safe="")
        assert c.get(path).status_code == 404, name
        assert c.delete(path).status_code == 404, name
        with pytest.raises(ValueError):
            datasets.import_csv_text(st, cid, name, "order_id\n1\n")
        with pytest.raises(KeyError):
            datasets.preview(st, cid, name)
        with pytest.raises(KeyError):
            datasets.delete(st, cid, name)
    assert dataset_names(c) == ["orders"]
    assert len(ok(c.get("/api/datasets/orders"))["rows"]) == 12
    assert datasets.query(st, cid, ["orders"], "SELECT COUNT(*) FROM orders").splitlines()[1] == "12"


def test_sql_injection_in_csv_headers_becomes_a_harmless_column_name(tmp_path):
    """A CSV header cell carrying SQL becomes a plain identifier column; no other table is touched."""
    c = web(tmp_path, PlaybookProvider())
    ok(upload(c, "orders", ORDERS_CSV))
    out = ok(upload(c, "customers", 'id,"name"" TEXT); DROP TABLE orders; --"\n1,Anna\n'))
    assert out["columns"] == ["id", "name_text_drop_table_orders"] and out["rows"] == 1
    assert ok(c.get("/api/datasets/customers"))["rows"] == [[1, "Anna"]]
    assert len(ok(c.get("/api/datasets/orders"))["rows"]) == 12


def test_oversized_csv_is_rejected_by_byte_size(tmp_path):
    """CSV uploads over 5 MB are rejected with 413, measured in bytes so multibyte text can't slip past."""
    c = web(tmp_path, PlaybookProvider())
    r = upload(c, "notes", "note\n" + ("x" * 999 + "\n") * 5005)  # ~5.0 MB of ASCII
    assert r.status_code == 413 and r.json()["detail"] == "CSV files are limited to 5 MB"
    r = upload(c, "notes", "note\n" + "å" * 2_600_000 + "\n")  # 2.6M characters, 5.2 MB of UTF-8
    assert r.status_code == 413 and r.json()["detail"] == "CSV files are limited to 5 MB"
    assert dataset_names(c) == []


def test_csv_with_too_many_rows_is_rejected(tmp_path):
    """A CSV over the row limit is rejected with a message stating the limit."""
    c = web(tmp_path, PlaybookProvider())
    r = upload(c, "events", "n\n" + "1\n" * (datasets.MAX_IMPORT_ROWS + 1))
    assert r.status_code == 422 and r.json()["detail"] == f"Too many rows (limit {datasets.MAX_IMPORT_ROWS})"
    assert dataset_names(c) == []


def test_row_limit_applies_to_data_rows(tmp_path, monkeypatch):
    """A CSV with exactly the stated row limit of data rows is accepted."""
    monkeypatch.setattr(datasets, "MAX_IMPORT_ROWS", 5)
    c = web(tmp_path, PlaybookProvider())
    r = upload(c, "events", "n\n" + "1\n" * 5)
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == 5


def test_rejected_reupload_keeps_the_existing_dataset(tmp_path):
    """A rejected replacement upload (empty, header-only, too many rows, too big) leaves the existing dataset unchanged."""
    c = web(tmp_path, PlaybookProvider())
    before = ok(upload(c, "orders", ORDERS_CSV))
    for bad, code in (("\n\n", 422), ("order_id,amount\n", 422), ("n\n" + "1\n" * (datasets.MAX_IMPORT_ROWS + 1), 422),
                      ("n\n" + "x" * 5_000_001, 413)):
        assert upload(c, "orders", bad).status_code == code
    (ds,) = ok(c.get("/api/data"))["datasets"]
    assert ds["rows"] == 12 and [col.split()[0] for col in ds["columns"]] == before["columns"]
    assert len(ok(c.get("/api/datasets/orders"))["rows"]) == 12


def test_short_rows_are_padded_with_nulls(tmp_path):
    """Rows with fewer cells than the header are kept, with the missing cells stored as NULL and numeric columns still numeric."""
    c = web(tmp_path, PlaybookProvider())
    out = ok(upload(c, "stock", "sku,qty,note\nA1,2\nB2,3,fragile\nC3\n"))
    assert out == {"name": "stock", "columns": ["sku", "qty", "note"], "rows": 3}
    assert ok(c.get("/api/datasets/stock"))["rows"] == [["A1", 2, None], ["B2", 3, "fragile"], ["C3", None, None]]
    assert ok(c.get("/api/data"))["datasets"][0]["columns"] == ["sku TEXT", "qty INTEGER", "note TEXT"]


def test_rows_with_extra_cells_are_not_silently_truncated(tmp_path):
    """A row with more cells than the header (e.g. an unquoted comma) is rejected with a message, not imported with data dropped."""
    c = web(tmp_path, PlaybookProvider())
    r = upload(c, "invoices", "customer,amount_sek\nAcme, Inc,100\nBeta AB,50\n")
    assert r.status_code == 422, r.text
    assert "row" in r.json()["detail"].lower()
    assert dataset_names(c) == []


def test_too_wide_csv_is_rejected_without_destroying_existing_data(tmp_path):
    """A CSV with more columns than the database supports is rejected with 422 and the existing dataset survives."""
    c = web(tmp_path, PlaybookProvider())
    ok(upload(c, "orders", ORDERS_CSV))
    wide = ",".join(f"c{i}" for i in range(2100)) + "\n" + ",".join("1" for _ in range(2100)) + "\n"
    r = upload(c, "orders", wide)
    assert r.status_code == 422, (r.status_code, r.text)
    preview = c.get("/api/datasets/orders")
    assert preview.status_code == 200 and len(preview.json()["rows"]) == 12


def test_underscored_codes_stay_text(tmp_path):
    """Codes such as 2026_01 are not numbers and are stored exactly as written."""
    c = web(tmp_path, PlaybookProvider())
    ok(upload(c, "batches", "batch,qty\n2026_01,5\n2026_02,7\n"))
    assert ok(c.get("/api/datasets/batches"))["rows"] == [["2026_01", 5], ["2026_02", 7]]


def test_blank_and_duplicate_headers_get_unique_usable_column_names(tmp_path):
    """Blank, duplicate, digit-leading and punctuated header cells become unique, queryable column names."""
    c = web(tmp_path, PlaybookProvider())
    out = ok(upload(c, "people", "name,,name,1st,Total (SEK)\nAnna,x,Berg,yes,100\nBo,y,Ek,no,250\n"))
    assert out["columns"] == ["name", "col", "name_2", "c_1st", "total_sek"]
    assert ok(c.get("/api/datasets/people"))["rows"] == [["Anna", "x", "Berg", "yes", 100], ["Bo", "y", "Ek", "no", 250]]
    st = Store(str(tmp_path / "web.db"))
    cid = st.get_or_create_customer("Nordhem Design")
    assert datasets.query(st, cid, ["people"], "SELECT SUM(total_sek) FROM people").splitlines()[1] == "350"
