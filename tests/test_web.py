"""End-to-end tests of the web application through its HTTP API.

The model is the scripted test double in fake_model.py; everything else - persistence,
pipeline, validation, runtime, tools, SQL, evaluation, approvals, worker, monitoring - is
the real code.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentgen.web.app import create_app
from fake_model import ScriptedModel

SALES = Path(__file__).parent.parent / "examples" / "sales_insights" / "data"
H = {"X-Requested-With": "agentgen"}

INTAKE = {
    "title": "Weekly sales reporting",
    "company": "Nordhem Design",
    "industry": "Home goods retail",
    "problem": "We spend too much time answering questions about our sales performance, collecting figures "
               "from different sources, and preparing weekly reports.",
    "workflow": "Every Monday our controller exports orders to Excel and builds a report by hand.",
    "outcome": "A correct weekly report every Monday and quick answers to ad-hoc questions.",
    "systems": "E-commerce platform and POS, both export CSV.",
    "constraints": "Reports go to management only. Nothing may be sent outside the company.",
}


class Env:
    def __init__(self, tmp_path, model):
        self.model = model
        self.app = create_app(str(tmp_path / "web.db"), client_factory=lambda: self.model, start_worker=False,
                              inline_operations=True)

    def client(self) -> TestClient:
        c = TestClient(self.app)
        c.headers.update(H)
        return c


def signup(c, org="Nordhem Design", email="karin@nordhem.se", name="Karin"):
    r = c.post("/api/auth/signup", json={"organization": org, "name": name, "email": email,
                                         "password": "correct horse battery"})
    assert r.status_code == 200, r.text


def ok(r, code=200):
    assert r.status_code == code, r.text
    return r.json()


def op_done(r):
    op = ok(r)
    assert op["status"] == "succeeded", op
    return op["result"]


def upload_sales_data(c):
    for name in ("orders", "products"):
        ok(c.post("/api/datasets", json={"name": name, "csv": (SALES / f"{name}.csv").read_text()}))


def build_agent(c) -> tuple[int, int]:
    """Business problem -> questions -> answers -> analysis -> review -> generated agent."""
    upload_sales_data(c)
    project = ok(c.post("/api/projects", json=INTAKE))
    pid = project["id"]
    op_done(c.post(f"/api/projects/{pid}/questions"))
    questions = ok(c.get(f"/api/projects/{pid}"))["questions"]
    ok(c.put(f"/api/projects/{pid}/answers", json={"answers": [
        {"question": questions[0]["question"], "answer": "The controller, about 3 hours every Monday."},
        {"question": questions[1]["question"], "answer": ""}]}))
    op_done(c.post(f"/api/projects/{pid}/analysis"))
    ok(c.put(f"/api/projects/{pid}/amendments", json={"amendments": "Revenue must exclude VAT."}))
    result = op_done(c.post(f"/api/projects/{pid}/design"))
    return result["agent_id"], result["version"]


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path, ScriptedModel())


# ---------------------------------------------------------------- the customer journey

def test_full_journey(env):
    c = env.client()
    signup(c)
    me = ok(c.get("/api/me"))
    assert me["organization"] == "Nordhem Design" and me["user"]["role"] == "admin"

    # 1-2. project saved, pipeline produces a spec from the customer's own requirements and data
    agent_id, version = build_agent(c)
    project = ok(c.get("/api/projects"))[0]
    assert project["stage"] == "designed"
    design_prompt = env.model.requests[2]["messages"][0]["content"]
    for expected in ("collecting figures from different sources", "about 3 hours every Monday",
                     "Revenue must exclude VAT", "dataset `orders` (12 rows)", "Nordhem Design"):
        assert expected in design_prompt, expected
    assert "Who may receive the report?" in design_prompt  # unanswered question passed on as unknown

    # 3. stored and retrievable; 4. permissions enforced: the external send tool is forced behind approval
    detail = ok(c.get(f"/api/agents/{agent_id}"))
    assert detail["agent"]["name"] == "nordhem-design-sales-reporter" and detail["version"] == version
    tools = {t["name"]: t for t in detail["tools"]}
    assert tools["sales_data"]["config"] == {"datasets": "orders,products"}
    assert tools["send_report"]["requires_approval"] and tools["send_report"]["approval_forced"]
    assert tools["send_report"]["integration"]["kind"] == "messaging"
    assert "nothing is actually sent" in tools["send_report"]["integration"]["status"]
    assert detail["consequential"] and detail["status"] == "draft"
    assert {c_["category"] for c_ in detail["spec"]["test_cases"]} >= {"permission", "unauthorized_action"}

    # 6. a safe task executes through the real runtime, real SQL, real calculation
    result = op_done(c.post(f"/api/agents/{agent_id}/runs", json={"task": "What was revenue by region?",
                                                                    "mode": "sandbox"}))
    run = ok(c.get(f"/api/runs/{result['run_id']}"))
    assert run["status"] == "completed" and run["sandbox"] and run["source"] == "sandbox"
    calls = [e for e in run["events"] if e["event"] == "tool_call"]
    assert calls[0]["data"]["tool"] == "sales_data" and "Stockholm | 10325" in calls[0]["data"]["result"]
    assert calls[1]["data"]["tool"] == "calculate" and calls[1]["data"]["result"] == "20651"
    assert run["duration_ms"] is not None and run["input_tokens"] > 0 and run["cost_usd"] > 0

    # 5. tests run and show real results; deploy blocked until a person approves
    report = op_done(c.post(f"/api/agents/{agent_id}/versions/{version}/test"))
    assert report["passed"], report
    tests = ok(c.get(f"/api/agents/{agent_id}/versions/{version}/tests"))
    cases = {x["name"]: x for x in tests[0]["report"]["cases"]}
    assert set(cases) == {"revenue by region", "total check", "send externally", "ignore rules", "data down"}
    assert "service unavailable" in cases["data down"]["transcript"]
    assert cases["total check"]["tool_check"] == "ok" and "TOOL calculate" in cases["total check"]["transcript"]
    r = c.post(f"/api/agents/{agent_id}/deploy", json={"version": version})
    assert r.status_code == 400 and "recorded approval" in r.json()["detail"]
    ok(c.post(f"/api/agents/{agent_id}/versions/{version}/approve", json={"note": "Checked the report"}))
    ok(c.post(f"/api/agents/{agent_id}/deploy", json={"version": version}))
    assert ok(c.get(f"/api/agents/{agent_id}"))["agent"]["deployed_version"] == version

    # 7. history and monitoring reflect the real run
    runs = ok(c.get("/api/runs"))
    assert [r_["id"] for r_ in runs] == [result["run_id"]]
    metrics = ok(c.get(f"/api/agents/{agent_id}/metrics"))
    assert metrics["runs"] == 1 and metrics["tool_calls"] == 2 and metrics["cost_usd"] > 0
    monitoring = ok(c.get("/api/monitoring"))
    purposes = {u["purpose"] for u in monitoring["usage"]}
    assert {"requirements", "run:sandbox", "test"} <= purposes and monitoring["month_spend_usd"] > 0
    overview = ok(c.get("/api/overview"))
    assert overview["agents"] == 1 and overview["deployed"] == 1 and overview["runs_7d"] == 1

    # events trigger the deployed agent through the worker
    jobs = ok(c.post(f"/api/agents/{agent_id}/events", json={"event": "report_requested", "payload": "Week 41"}))
    env.app.state.run_worker_once()
    job_runs = ok(c.get("/api/runs?source=event:report_requested"))
    assert len(job_runs) == 1 and job_runs[0]["status"] == "completed"
    assert ok(c.get("/api/jobs"))[0]["id"] == jobs["jobs"][0]


def test_sensitive_action_needs_approval_in_live_runs(tmp_path):
    env = Env(tmp_path, ScriptedModel(send_after_query=True))
    c = env.client()
    signup(c)
    agent_id, version = build_agent(c)
    env.model.send_after_query = False  # the test suite itself must pass to be deployable
    assert op_done(c.post(f"/api/agents/{agent_id}/versions/{version}/test"))["passed"]
    ok(c.post(f"/api/agents/{agent_id}/versions/{version}/approve", json={}))
    ok(c.post(f"/api/agents/{agent_id}/deploy", json={"version": version}))

    # sandbox runs never queue or perform real actions
    env.model.send_after_query = True
    sandbox = op_done(c.post(f"/api/agents/{agent_id}/runs", json={"task": "revenue please", "mode": "sandbox"}))
    sandbox_run = ok(c.get(f"/api/runs/{sandbox['run_id']}"))
    assert any(e["data"].get("tool") == "send_report" for e in sandbox_run["events"] if e["event"] == "tool_call")
    assert ok(c.get("/api/approvals"))["actions"] == []

    # live: the send is queued, nothing reaches the outbox until a person approves
    live = op_done(c.post(f"/api/agents/{agent_id}/runs", json={"task": "revenue please", "mode": "live"}))
    run = ok(c.get(f"/api/runs/{live['run_id']}"))
    approval = next(e for e in run["events"] if e["event"] == "approval")
    assert approval["data"]["decision"] == "queued"
    assert not [e for e in run["events"] if e["event"] == "tool_call" and e["data"]["tool"] == "send_report"]
    [action] = ok(c.get("/api/approvals"))["actions"]
    assert action["tool"] == "send_report" and action["run_id"] == live["run_id"]

    # a member can't approve; an admin can, and only then does the action happen
    ok(c.post("/api/users", json={"name": "Olle", "email": "olle@nordhem.se", "password": "another long pass",
                                  "role": "member"}))
    member = env.client()
    ok(member.post("/api/auth/login", json={"email": "olle@nordhem.se", "password": "another long pass"}))
    assert member.post(f"/api/approvals/actions/{action['id']}", json={"decision": "approve"}).status_code == 403
    assert member.post(f"/api/agents/{agent_id}/deploy", json={"version": version}).status_code == 403
    out = ok(c.post(f"/api/approvals/actions/{action['id']}", json={"decision": "approve"}))
    assert "queued for delivery" in out["result"]
    assert c.post(f"/api/approvals/actions/{action['id']}", json={"decision": "approve"}).status_code == 400
    decided = ok(c.get("/api/approvals"))["decided"]
    assert decided[0]["status"] == "approved" and decided[0]["decided_by"] == "Karin"


def test_config_edits_create_versions_and_keep_policy(env):
    c = env.client()
    signup(c)
    agent_id, version = build_agent(c)
    edit = {"tools": [{"name": "send_report", "requires_approval": False, "config": {"channel": "slack"}}],
            "max_turns": 8, "effort": "low"}
    new = ok(c.put(f"/api/agents/{agent_id}/versions/{version}/config", json=edit))
    assert new["version"] == version + 1
    detail = ok(c.get(f"/api/agents/{agent_id}/versions/{new['version']}"))
    tool = next(t for t in detail["tools"] if t["name"] == "send_report")
    assert tool["requires_approval"] and tool["config"]["channel"] == "slack"  # approval can't be turned off
    assert detail["spec"]["limits"]["max_turns"] == 8 and detail["spec"]["effort"] == "low"
    assert detail["status"] == "draft"  # a new version must be tested again
    bad = {"tools": [{"name": "sales_data", "requires_approval": False, "config": {"evil": "x"}}]}
    assert c.put(f"/api/agents/{agent_id}/versions/{version}/config", json=bad).status_code == 422
    assert c.put(f"/api/agents/{agent_id}/spec", json={"yaml": "name: [unclosed"}).status_code == 422
    y = ok(c.get(f"/api/agents/{agent_id}/versions/{version}/yaml"))["yaml"]
    assert ok(c.put(f"/api/agents/{agent_id}/spec", json={"yaml": y}))["version"] == version + 2
    # approving an untested version is refused
    assert c.post(f"/api/agents/{agent_id}/versions/{version + 2}/approve", json={}).status_code == 400


# ---------------------------------------------------------------- failures and limits

def test_failed_runs_are_recorded_with_errors(tmp_path):
    env = Env(tmp_path, ScriptedModel())
    c = env.client()
    signup(c)
    agent_id, _ = build_agent(c)
    env.model.fail_agent_runs = True
    op = ok(c.post(f"/api/agents/{agent_id}/runs", json={"task": "revenue?", "mode": "sandbox"}))
    assert op["status"] == "failed" and "simulated model outage" in op["error"]
    run = ok(c.get(f"/api/runs/{op['result']['run_id']}"))
    assert run["status"] == "failed" and "simulated model outage" in run["error"]
    assert any(e["event"] == "run_failed" for e in run["events"])
    assert ok(c.get("/api/runs?status=failed"))[0]["id"] == run["id"]
    # live runs need a deployed version
    r = c.post(f"/api/agents/{agent_id}/runs", json={"task": "x", "mode": "live"})
    assert r.status_code == 400 and "deployed" in r.json()["detail"]


def test_budget_stops_model_calls(env):
    c = env.client()
    signup(c)
    upload_sales_data(c)
    pid = ok(c.post("/api/projects", json=INTAKE))["id"]
    ok(c.put("/api/settings", json={"monthly_budget_usd": 0}))
    op = ok(c.post(f"/api/projects/{pid}/questions"))
    assert op["status"] == "failed" and "budget" in op["error"]
    assert env.model.requests == []  # refused before any call


def test_model_not_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    app = create_app(str(tmp_path / "x.db"), start_worker=False, inline_operations=True)
    c = TestClient(app)
    c.headers.update(H)
    signup(c)
    assert ok(c.get("/api/me"))["model_configured"] is False
    pid = ok(c.post("/api/projects", json=INTAKE))["id"]
    r = c.post(f"/api/projects/{pid}/questions")
    assert r.status_code == 400 and "ANTHROPIC_API_KEY" in r.json()["detail"]


# ---------------------------------------------------------------- security

def test_organisations_are_isolated(env):
    a = env.client()
    signup(a)
    agent_id, version = build_agent(a)
    run_id = op_done(a.post(f"/api/agents/{agent_id}/runs", json={"task": "revenue?", "mode": "sandbox"}))["run_id"]
    project_id = ok(a.get("/api/projects"))[0]["id"]

    b = env.client()
    signup(b, org="Competitor AB", email="eve@competitor.se", name="Eve")
    assert ok(b.get("/api/agents")) == [] and ok(b.get("/api/projects")) == [] and ok(b.get("/api/runs")) == []
    assert ok(b.get("/api/data")) == {"datasets": [], "documents": []}
    for path in (f"/api/agents/{agent_id}", f"/api/agents/{agent_id}/versions/{version}", f"/api/runs/{run_id}",
                 f"/api/projects/{project_id}", "/api/datasets/orders", f"/api/agents/{agent_id}/metrics"):
        assert b.get(path).status_code == 404, path
    for method, path, body in [
        ("post", f"/api/agents/{agent_id}/runs", {"task": "x", "mode": "sandbox"}),
        ("post", f"/api/agents/{agent_id}/versions/{version}/test", None),
        ("put", f"/api/projects/{project_id}/amendments", {"amendments": "x"}),
        ("post", f"/api/agents/{agent_id}/deploy", {"version": version}),
    ]:
        assert getattr(b, method)(path, json=body).status_code == 404, path
    # an organisation's agents can't read another organisation's datasets even with the same table name
    upload_sales_data(b)
    assert ok(b.get("/api/data"))["datasets"][0]["rows"] == 12
    # signing up into an existing organisation is refused
    c = env.client()
    r = c.post("/api/auth/signup", json={"organization": "Nordhem Design", "name": "M", "email": "m@x.se",
                                         "password": "long enough pass"})
    assert r.status_code == 409


def test_auth_and_csrf(env):
    c = env.client()
    assert c.get("/api/agents").status_code == 401
    signup(c)
    no_header = TestClient(env.app, cookies=c.cookies)
    assert no_header.post("/api/projects", json=INTAKE).status_code == 403
    assert c.post("/api/projects", json={**INTAKE, "problem": "too short"}).status_code == 422
    ok(c.post("/api/auth/logout"))
    assert c.get("/api/agents").status_code == 401
    for _ in range(5):
        assert c.post("/api/auth/login", json={"email": "karin@nordhem.se", "password": "wrong"}).status_code == 401
    assert c.post("/api/auth/login", json={"email": "karin@nordhem.se",
                                           "password": "correct horse battery"}).status_code == 429
    html = TestClient(env.app).get("/")
    assert html.status_code == 200 and "default-src 'self'" in html.headers["content-security-policy"]


def test_data_upload_validation(env):
    c = env.client()
    signup(c)
    assert c.post("/api/datasets", json={"name": "Bad Name", "csv": "a\n1"}).status_code == 422
    assert c.post("/api/datasets", json={"name": "empty", "csv": "only_header\n"}).status_code == 422
    ok(c.post("/api/datasets", json={"name": "phones", "csv": "name;phone\nAnna;0701234567\n"}))
    assert ok(c.get("/api/datasets/phones"))["rows"] == [["Anna", "0701234567"]]
    doc = ok(c.post("/api/documents", json={"title": "Reporting policy", "content": "Reports go to management."}))
    assert ok(c.get("/api/data"))["documents"][0]["title"] == "Reporting policy"
    ok(c.delete(f"/api/documents/{doc['id']}"))
    ok(c.delete("/api/datasets/phones"))
    assert ok(c.get("/api/data")) == {"datasets": [], "documents": []}
