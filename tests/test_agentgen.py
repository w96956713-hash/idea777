"""Offline tests: a scripted fake client stands in for the Claude API."""

import io
import json
import urllib.request
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentgen import components, datasets
from agentgen.cli import main
from agentgen.components import CATALOG, Context, ToolFailure, is_consequential
from agentgen.designer import validate_design
from agentgen.evaluate import Verdict, check_tools, evaluate
from agentgen.monitor import agent_metrics
from agentgen.pipeline import Analysis, Discovery, analyze, design, generate_questions, record_answers
from agentgen.runtime import AgentRuntime, approve_all, execute_queued_action, queue_all
from agentgen.spec import AgentDesign, AgentSpec, ConfigItem, ToolSpec
from agentgen.spec import TestCase as Case
from agentgen.store import Store, utcnow
from agentgen.worker import fire_event, run_due_jobs, schedule_due

EXAMPLES = Path(__file__).parent.parent / "examples"
PROPERTY = EXAMPLES / "property_requests"
SALES = EXAMPLES / "sales_insights"


# ---------------------------------------------------------------- fakes

def text(t):
    return SimpleNamespace(type="text", text=t)


def tool_use(name, args, id="t1"):
    return SimpleNamespace(type="tool_use", name=name, input=args, id=id)


def response(*blocks, stop="end_turn", tokens=(10, 5)):
    return SimpleNamespace(content=list(blocks), stop_reason=stop,
                           usage=SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1]))


class FakeClient:
    """create() returns scripted agent responses; stream() returns structured outputs by type."""

    def __init__(self, responses=(), structured=None, verdict=True):
        self.responses = list(responses)
        self.structured = dict(structured or {})
        self.verdict = verdict
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create, stream=self._stream))

    def _create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @contextmanager
    def _stream(self, **kwargs):
        fmt = kwargs["output_format"]
        if fmt is Verdict:
            parsed = Verdict(reasoning="ok", passed=self.verdict)
        else:
            parsed = self.structured[fmt]
        yield SimpleNamespace(get_final_message=lambda: SimpleNamespace(stop_reason="end_turn", parsed_output=parsed))


# ---------------------------------------------------------------- fixtures

def load(path: Path) -> AgentSpec:
    return AgentSpec.from_yaml(path.read_text(encoding="utf-8"))


@pytest.fixture
def store(tmp_path):
    return Store(":memory:", data_dir=str(tmp_path / "data"))


@pytest.fixture
def router(store):
    """The property-request router with its datasets and policy loaded."""
    customer_id = store.get_or_create_customer("Hemvist")
    datasets.import_csv(store, customer_id, "tenants", PROPERTY / "data" / "tenants.csv")
    datasets.import_csv(store, customer_id, "properties", PROPERTY / "data" / "properties.csv")
    store.save_version(customer_id, validate_design(load(PROPERTY / "spec.yaml")))
    agent_id = store.agent("service-request-router")["id"]
    store.add_knowledge(agent_id, "routing_policy", (PROPERTY / "knowledge" / "routing_policy.md").read_text())
    return store


def runtime(store, client, name="service-request-router", approver=approve_all, case=None, version=None):
    spec, v = store.load_spec(name, version)
    return AgentRuntime(client, store, store.agent(name)["id"], spec, v, approver, test_case=case)


# ---------------------------------------------------------------- spec & validation

def test_v1_spec_is_upgraded():
    spec = load(EXAMPLES / "accounting_firm" / "spec.yaml")
    names = [t.name for t in spec.tools]
    assert "send_message" in names and "send_email" not in names
    assert spec.tool("send_message").config_dict()["channel"] == "email"
    assert spec.test_cases[0].must_call == ["search_knowledge", "send_message"]
    assert spec.limits.max_turns == 20 and spec.memory.enabled is False


def test_examples_validate_cleanly():
    for path in (PROPERTY / "spec.yaml", SALES / "spec.yaml"):
        spec = load(path)
        before = {t.name for t in spec.tools}
        spec = validate_design(spec)
        assert {t.name for t in spec.tools} == before
        assert not [q for q in spec.open_questions if "removed" in q]


def test_validation_enforces_policy():
    spec = validate_design(AgentSpec(**AgentDesign(
        name="x", purpose="p", capabilities=[], instructions="i", workflow=[], data_sources=[], triggers=[],
        memory={"enabled": False, "what_to_remember": "", "retention_days": 0},
        limits={"max_turns": 500, "max_tokens_per_run": 10},
        tools=[
            ToolSpec(name="pay", component="http_request", purpose="", requires_approval=False, config=[
                ConfigItem(key="base_url", value="https://bank.example"), ConfigItem(key="method", value="POST"),
                ConfigItem(key="path", value="/payments"), ConfigItem(key="effect", value="financial")]),
            ToolSpec(name="bad api", component="http_request", purpose="", requires_approval=True, config=[
                ConfigItem(key="base_url", value="http://insecure.example"), ConfigItem(key="method", value="GET"),
                ConfigItem(key="path", value="/")]),
            ToolSpec(name="teleport", component="teleporter", purpose="", requires_approval=False, config=[]),
            ToolSpec(name="mem", component="remember", purpose="", requires_approval=False, config=[]),
        ],
        success_criteria=[], restrictions=[], assumptions=[], open_questions=[],
        test_cases=[Case(name="c", category="task", input="i", expected_behavior="e", must_call=["teleport", "pay"],
                         must_not_call=[], fail_tools=[], fixtures=[])],
    ).model_dump()))
    names = [t.name for t in spec.tools]
    assert names == ["pay", "escalate_to_human"]
    assert spec.tool("pay").requires_approval is True  # financial: forced
    assert spec.test_cases[0].must_call == ["pay"]
    assert spec.limits.max_turns == 50 and spec.limits.max_tokens_per_run == 20000
    notes = " ".join(spec.open_questions)
    for expected in ("teleporter", "https", "memory is disabled", "permission", "api_failure"):
        assert expected in notes


# ---------------------------------------------------------------- data access

def test_dataset_import_and_read_only_queries(router):
    cid = router.agent("service-request-router")["customer_id"]
    out = datasets.query(router, cid, ["tenants", "properties"],
                         "SELECT t.name, p.address FROM tenants t JOIN properties p USING (property_id) WHERE apartment = 1102")
    assert "Sara Lindqvist | Kopparbergsvägen 12" in out
    for sql in ("DELETE FROM tenants", "DROP TABLE tenants", "UPDATE tenants SET name = 'x'",
                "PRAGMA table_info(tenants)", "ATTACH DATABASE 'x.db' AS x"):
        assert datasets.query(router, cid, ["tenants"], sql).startswith("Error"), sql
    assert datasets.query(router, cid, ["tenants"], "SELECT * FROM properties").startswith("Error")
    assert "5" in datasets.query(router, cid, ["tenants"], "SELECT COUNT(*) FROM tenants")
    # identifiers keep leading zeros; real numbers stay numeric
    assert "0904 | 0701234503" in datasets.query(router, cid, ["tenants"],
                                                 "SELECT apartment, phone FROM tenants WHERE tenant_id = 'T-1003'")
    assert "1932" in datasets.query(router, cid, ["properties"],
                                    "SELECT building_year FROM properties WHERE building_year < 1950")


def test_customer_datasets_are_isolated(router, tmp_path):
    other = router.get_or_create_customer("Other AB")
    assert datasets.query(router, other, ["tenants"], "SELECT * FROM tenants").startswith("Error")
    assert router.datasets(other) == []


def test_query_data_tool_describes_schema(router):
    spec, _ = router.load_spec("service-request-router")
    rt = runtime(router, FakeClient())
    lookup = next(t for t in rt.tools if t["name"] == "lookup_tenant")
    assert "tenants (5 rows)" in lookup["description"] and "contract_status TEXT" in lookup["description"]
    assert lookup["strict"] is True
    dispatch = next(t for t in rt.tools if t["name"] == "dispatch_contractor")
    assert dispatch["input_schema"]["required"] == ["address", "apartment", "problem", "priority", "case_id"]
    assert "strict" not in dispatch


# ---------------------------------------------------------------- components

def ctx_for(store, tool, sandbox=False, retention=0):
    agent = store.conn.execute("SELECT * FROM agents LIMIT 1").fetchone()
    return Context(store, agent["id"], agent["customer_id"], tool, sandbox=sandbox, memory_retention_days=retention)


def test_http_request(router, monkeypatch):
    spec, _ = router.load_spec("service-request-router")
    tool = spec.tool("dispatch_contractor")
    http = CATALOG["http_request"]
    assert http.risk_for(tool) == "financial"
    with pytest.raises(ToolFailure, match="secret FASTIGHETSDRIFT_TOKEN"):
        http.run({"address": "a", "apartment": "1", "problem": "p", "priority": "urgent", "case_id": "1"},
                 ctx_for(router, tool))
    with pytest.raises(ToolFailure, match="tests never call real systems"):
        http.run({}, ctx_for(router, tool, sandbox=True))

    monkeypatch.setenv("FASTIGHETSDRIFT_TOKEN", "secret-token")
    sent = {}

    class Resp(io.BytesIO):
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def fake_urlopen(request, timeout):
        sent.update(url=request.full_url, auth=request.get_header("Authorization"), body=json.loads(request.data))
        return Resp(b'{"work_order_id": "WO-1"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = http.run({"address": "Kopparbergsvägen 12", "apartment": "1102", "problem": "leak", "priority": "urgent",
                    "case_id": "7"}, ctx_for(router, tool))
    assert out.startswith("HTTP 201") and "WO-1" in out
    assert sent["url"] == "https://api.fastighetsdrift.example/v1/work-orders"
    assert sent["auth"] == "Bearer secret-token" and sent["body"]["case_id"] == "7"


def test_http_path_params_cannot_change_host():
    tool = ToolSpec(name="get", component="http_request", purpose="", requires_approval=False, config=[
        ConfigItem(key="base_url", value="https://api.example/v1"), ConfigItem(key="method", value="GET"),
        ConfigItem(key="path", value="/customers/{id}")])
    req = CATALOG["http_request"].build_request({"id": "../../evil.example/x", "q": "a b"}, tool.config_dict())
    assert req.full_url.startswith("https://api.example/v1/customers/..%2F..%2Fevil.example%2Fx?q=a+b")


def test_record_components(router):
    spec, _ = router.load_spec("service-request-router")
    create, update = CATALOG["create_record"], CATALOG["update_record"]
    with pytest.raises(ToolFailure, match="missing required fields"):
        create.run({"fields_json": '{"summary": "x"}'}, ctx_for(router, spec.tool("create_case")))
    out = create.run({"fields_json": json.dumps({"property_id": "P-01", "apartment": "1102", "department": "maintenance",
                                                 "priority": "normal", "summary": "tap"})},
                     ctx_for(router, spec.tool("create_case")))
    record_id = int(out.split("#")[1].rstrip("."))
    with pytest.raises(ToolFailure, match="status must be one of"):
        update.run({"record_id": record_id, "changes_json": "{}", "status": "deleted"}, ctx_for(router, spec.tool("update_case")))
    update.run({"record_id": record_id, "changes_json": '{"note": "worse"}', "status": "waiting"},
               ctx_for(router, spec.tool("update_case")))
    found = CATALOG["search_records"].run({"text": "1102"}, ctx_for(router, spec.tool("find_open_cases")))
    assert f"#{record_id} [waiting]" in found and "worse" in found


def test_utility_components(router):
    calc = ToolSpec(name="calc", component="calculate", purpose="", requires_approval=False, config=[])
    assert CATALOG["calculate"].run({"expression": "round((10274 - 10377) / 10377 * 100, 2)"}, ctx_for(router, calc)) == "-0.99"
    with pytest.raises(ToolFailure):
        CATALOG["calculate"].run({"expression": "__import__('os').system('ls')"}, ctx_for(router, calc))
    mem = ToolSpec(name="m", component="remember", purpose="", requires_approval=False, config=[])
    with pytest.raises(ToolFailure, match="disabled"):
        CATALOG["remember"].run({"key": "k", "content": "c"}, ctx_for(router, mem))
    CATALOG["remember"].run({"key": "weekly-2026-10-10", "content": "revenue 10274"}, ctx_for(router, mem, retention=30))
    assert "10274" in CATALOG["recall"].run({"query": "weekly"}, ctx_for(router, mem))
    send = ToolSpec(name="s", component="send_message", purpose="", requires_approval=True,
                    config=[ConfigItem(key="channel", value="email"), ConfigItem(key="allowed_recipients", value="@hemvist.se")])
    with pytest.raises(ToolFailure, match="not an allowed recipient"):
        CATALOG["send_message"].run({"to": "x@evil.se", "subject": "s", "body": "b"}, ctx_for(router, send))
    assert "queued for delivery" in CATALOG["send_message"].run({"to": "a@hemvist.se", "subject": "s", "body": "b"},
                                                                  ctx_for(router, send))


# ---------------------------------------------------------------- runtime

def test_runtime_uses_data_and_records(router):
    client = FakeClient([
        response(tool_use("lookup_tenant", {"sql": "SELECT tenant_id, property_id FROM tenants WHERE apartment = '0904'"}),
                 stop="tool_use"),
        response(tool_use("create_case", {"fields_json": json.dumps({
            "property_id": "P-02", "apartment": "0904", "department": "maintenance", "priority": "normal",
            "summary": "Dripping tap"})}, id="t2"), stop="tool_use"),
        response(text("Created a normal maintenance case.")),
    ])
    result = runtime(router, client).run("Kranen droppar, Eva Berg 0904")
    assert "T-1003 | P-02" in result.tool_calls[0].result
    assert result.tool_calls[1].result.startswith("Created case #")
    assert result.called == {"lookup_tenant", "create_case"}
    events = [r["event"] for r in router.conn.execute("SELECT event FROM audit_log ORDER BY id")]
    assert events == ["run_started", "tool_call", "tool_call", "run_finished"]
    # the tool result goes back to the model as a user turn
    assert client.requests[1]["messages"][-1]["content"][0]["type"] == "tool_result"
    assert "Workflow:" in client.requests[0]["system"] and "Hard restrictions" in client.requests[0]["system"]


def test_queued_approval_then_person_approves(router):
    client = FakeClient([
        response(tool_use("notify_tenant", {"to": "0701234503", "subject": "", "body": "Ärende registrerat"}),
                 stop="tool_use"),
        response(text("Message awaits approval.")),
    ])
    result = runtime(router, client, approver=queue_all).run("x")
    assert result.tool_calls[0].approval == "queued" and "notify_tenant" not in result.called
    assert router.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    action = router.pending_actions()[0]
    assert "queued for delivery" in execute_queued_action(router, action["id"], "Anna", approve=True)
    assert router.conn.execute("SELECT status, channel FROM outbox").fetchone()[:] == ("pending_delivery", "sms")
    with pytest.raises(ValueError, match="already approved"):
        execute_queued_action(router, action["id"], "Anna", approve=True)


def test_denied_and_blocked_tools(router):
    client = FakeClient([
        response(tool_use("dispatch_contractor", {"address": "a", "apartment": "1", "problem": "p",
                                                  "priority": "urgent", "case_id": "1"}),
                 tool_use("delete_everything", {}, id="t2"), stop="tool_use"),
        response(text("Escalating.")),
    ])
    result = runtime(router, client, approver=lambda *_: False).run("x")
    assert [c.approval for c in result.tool_calls] == ["denied", "denied"]
    assert "not available" in result.tool_calls[1].result
    assert client.requests[1]["messages"][-1]["content"][1]["is_error"] is True


def test_test_mode_fixtures_and_simulated_outage(router):
    spec, _ = router.load_spec("service-request-router")
    leak = next(c for c in spec.test_cases if c.name == "urgent leak")
    outage = next(c for c in spec.test_cases if c.name == "contractor api down")
    call = tool_use("dispatch_contractor", {"address": "a", "apartment": "1", "problem": "p", "priority": "urgent",
                                            "case_id": "1"})
    r1 = runtime(router, FakeClient([response(call, stop="tool_use"), response(text("ok"))]), case=leak).run("x")
    assert "WO-5531" in r1.tool_calls[0].result
    r2 = runtime(router, FakeClient([response(call, stop="tool_use"), response(text("ok"))]), case=outage).run("x")
    assert r2.tool_calls[0].error and "service unavailable" in r2.tool_calls[0].result


def test_token_budget_stops_run_and_escalates(router):
    spec, _ = router.load_spec("service-request-router")
    spec.limits.max_tokens_per_run = 20000
    calls = [response(tool_use("routing_policy", {"query": "läcka"}, id=f"t{i}"), stop="tool_use", tokens=(15000, 1000))
             for i in range(5)]
    rt = AgentRuntime(FakeClient(calls), router, router.agent(spec.name)["id"], spec, 1, approve_all)
    result = rt.run("x")
    assert result.stop_reason == "budget_exceeded" and len(result.tool_calls) == 2
    assert router.conn.execute("SELECT reason FROM escalations").fetchone()[0] == "Run stopped: budget_exceeded"


# ---------------------------------------------------------------- evaluation & release

def _passing_responses(n):
    return [response(text("done")) for _ in range(n)]


def test_check_tools():
    case = Case(name="c", category="task", input="i", expected_behavior="e", must_call=["a"], must_not_call=["b"],
                fail_tools=[], fixtures=[])
    assert check_tools(case, {"a"}, {"a"}) == ""
    assert "did not use a" in check_tools(case, set(), set())
    assert "attempted forbidden b" in check_tools(case, {"a"}, {"a", "b"})


def test_safety_failures_block_release(store):
    customer = store.get_or_create_customer("Nordhem")
    datasets.import_csv(store, customer, "orders", SALES / "data" / "orders.csv")
    datasets.import_csv(store, customer, "products", SALES / "data" / "products.csv")
    spec = validate_design(load(SALES / "spec.yaml"))
    spec.test_cases = [c for c in spec.test_cases if c.name in ("margin question", "send externally")]
    store.save_version(customer, spec)
    report = evaluate(FakeClient(_passing_responses(2), verdict=False), store, spec.name)
    assert not report.passed and report.safety_failures == ["send externally"]
    assert report.avg_usd_per_run is not None


def test_regression_suite_and_approval_gate(store):
    customer = store.get_or_create_customer("Nordhem")
    datasets.import_csv(store, customer, "orders", SALES / "data" / "orders.csv")
    spec = validate_design(load(SALES / "spec.yaml"))
    spec.test_cases = [c for c in spec.test_cases if c.name in ("margin question", "send externally")]
    store.save_version(customer, spec)
    assert evaluate(FakeClient(_passing_responses(2)), store, spec.name, 1).passed

    v2 = spec.model_copy(deep=True)
    v2.test_cases = [c for c in v2.test_cases if c.name == "margin question"]
    store.save_version(customer, v2)
    report = evaluate(FakeClient(_passing_responses(2)), store, spec.name, 2)
    assert [(c.name, c.carried) for c in report.cases] == [("margin question", False), ("send externally", True)]

    v3 = spec.model_copy(deep=True)
    store.save_version(customer, v3)
    failing = FakeClient(_passing_responses(2))
    failing.verdict = False
    report = evaluate(failing, store, spec.name, 3)
    assert set(report.regressions) == {"margin question", "send externally"} and not report.passed

    # sales agent only drafts: not consequential, deploys without a recorded approval
    assert not is_consequential(spec.tools)
    store.deploy(spec.name, 2)
    with pytest.raises(ValueError, match="only passed versions"):
        store.deploy(spec.name, 3)


def test_consequential_agent_needs_recorded_approval(router):
    spec, _ = router.load_spec("service-request-router")
    assert is_consequential(spec.tools)
    one_case = spec.model_copy(deep=True)
    one_case.test_cases = one_case.test_cases[5:6]  # the injected-instruction case
    router.save_version(router.agent(spec.name)["customer_id"], one_case)
    client = FakeClient([response(tool_use("create_case", {"fields_json": json.dumps({
        "property_id": "P-03", "apartment": "2201", "department": "maintenance", "priority": "normal",
        "summary": "Stairwell lamp"})}), stop="tool_use"), response(text("Case created."))])
    report = evaluate(client, router, spec.name, 2)
    assert report.passed
    # the test wrote to the sandbox, not the real records
    assert router.conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
    with pytest.raises(ValueError, match="needs a recorded approval"):
        router.deploy(spec.name, 2, requires_approval=True)
    with pytest.raises(ValueError, match="approve only versions that passed"):
        router.approve_version(spec.name, 1, "Anna", "")
    router.approve_version(spec.name, 2, "Anna Berg", "Reviewed test report")
    router.deploy(spec.name, 2, requires_approval=True)
    assert router.deployed_spec(spec.name)[1] == 2


# ---------------------------------------------------------------- triggers & jobs

def _deploy_sales(store):
    customer = store.get_or_create_customer("Nordhem")
    datasets.import_csv(store, customer, "orders", SALES / "data" / "orders.csv")
    spec = validate_design(load(SALES / "spec.yaml"))
    store.save_version(customer, spec)
    store.record_test_run(spec.name, 1, 1.0, True, {"cases": []})
    store.deploy(spec.name, 1)
    return spec


def test_schedule_creates_and_runs_jobs(store):
    _deploy_sales(store)
    assert len(schedule_due(store)) == 1
    assert schedule_due(store) == []  # one pending job per schedule
    outcomes = run_due_jobs(FakeClient([response(text("Report drafted."))]), store)
    assert outcomes[0]["status"] == "done"
    assert store.conn.execute("SELECT trigger_source FROM runs").fetchone()[0] == "schedule:0"
    next_jobs = schedule_due(store)
    due = store.conn.execute("SELECT due_at FROM jobs WHERE id = ?", (next_jobs[0],)).fetchone()[0]
    assert due > (utcnow() + timedelta(days=6)).isoformat()


def test_event_trigger_and_retries(router):
    router.record_test_run("service-request-router", 1, 1.0, True, {"cases": []})
    router.approve_version("service-request-router", 1, "Anna", "")
    router.deploy("service-request-router", 1, requires_approval=True)
    with pytest.raises(ValueError, match="no trigger"):
        fire_event(router, "service-request-router", "unknown_event", "{}")
    [job] = fire_event(router, "service-request-router", "new_request", '{"text": "Ingen värme"}')
    assert "Ingen värme" in router.conn.execute("SELECT input FROM jobs WHERE id = ?", (job,)).fetchone()[0]
    failing = FakeClient([RuntimeError("API overloaded")] * 3)
    for _ in range(3):
        outcome = run_due_jobs(failing, router, now=utcnow() + timedelta(hours=1))
    assert outcome[0]["status"] == "failed"
    assert router.conn.execute("SELECT reason FROM escalations").fetchone()[0] == "Background job failed"


# ---------------------------------------------------------------- pipeline

def test_pipeline_stages_are_persisted(store):
    customer = store.get_or_create_customer("Hemvist")
    project = store.create_project(customer, "Requests", (PROPERTY / "intake.md").read_text())
    designed = load(PROPERTY / "spec.yaml")
    client = FakeClient(structured={
        Discovery: Discovery(questions=[{"topic": "volumes", "question": "How many urgent cases per week?", "why": "sizing"}]),
        Analysis: Analysis(process_summary="p", opportunities=[], recommended_agent="router",
                           recommended_capabilities=[], keep_human=["rent disputes"], integrations=[], data_needed=[],
                           risks=[], complexity="medium", complexity_reasons=[], estimated_runs_per_month=2600,
                           open_questions=[]),
        AgentDesign: AgentDesign(**designed.model_dump(exclude={"model", "effort"})),
    })
    assert generate_questions(client, store, project).questions[0].topic == "volumes"
    record_answers(store, project, [{"question": "How many urgent cases per week?", "answer": "About 15"}])
    analyze(client, store, project)
    spec, version = design(client, store, project)
    row = store.project(project)
    assert row["stage"] == "designed" and row["agent_name"] == spec.name and version == 1
    assert json.loads(row["analysis_json"])["estimated_runs_per_month"] == 2600
    _, version2 = design(client, store, project)  # redesign = next version of the same agent
    assert version2 == 2


# ---------------------------------------------------------------- monitoring & CLI

def test_monitor(router):
    client = FakeClient([
        response(tool_use("dispatch_contractor", {"address": "a", "apartment": "1", "problem": "p",
                                                  "priority": "urgent", "case_id": "1"}), stop="tool_use",
                 tokens=(1000, 100)),
        response(text("Waiting for approval."), tokens=(1000, 100)),
    ])
    runtime(router, client, approver=queue_all).run("x")
    m = agent_metrics(router, "service-request-router")
    assert m["runs"] == 1 and m["completion_rate"] == 1.0
    assert m["approvals"] == {"queued": 1} and m["pending_actions"] == 1
    assert m["cost_usd"] == pytest.approx((2000 * 4 + 200 * 20) / 1e6)


def test_cli_flow(tmp_path, capsys):
    db = str(tmp_path / "t.db")
    main(["--db", db, "data", "import", "Hemvist", "tenants", str(PROPERTY / "data" / "tenants.csv")])
    main(["--db", db, "import", str(PROPERTY / "spec.yaml"), "--customer", "Hemvist"])
    main(["--db", db, "import", str(EXAMPLES / "accounting_firm" / "spec.yaml"), "--customer", "Andersson"])
    main(["--db", db, "tools", "service-request-router"])
    out = capsys.readouterr().out
    assert "Imported 5 rows" in out and "dispatch_contractor" in out and "financial" in out
    exported = tmp_path / "out.yaml"
    main(["--db", db, "export", "service-request-router", "-o", str(exported)])
    assert AgentSpec.from_yaml(exported.read_text()).tool("dispatch_contractor").requires_approval
    main(["--db", db, "list"])
    assert "client-email-triage" in capsys.readouterr().out
