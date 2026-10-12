"""The agent lifecycle beyond the first release, through the web API: managing test cases,
diagnosing failures, proposing and applying fixes, retesting, restoring earlier versions, and the
rules that keep simulated (scripted-model) results from qualifying a version for real use.

The model is the scripted test double (labelled simulated); everything else is the real code.
"""

import pytest
from fastapi.testclient import TestClient

from agentgen.providers import ModelRateLimited
from agentgen.store import Store
from agentgen.web.app import create_app
from fake_model import ScriptedProvider
from scenario_kit import SALES, load_spec
from test_web import H, Env, build_agent, ok, op_done, signup

NEW_CASE = {"name": "orders this week", "category": "quality", "input": "How many orders were placed?",
            "expected_behavior": "Counts the orders from the data.", "must_call": ["sales_data"]}


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path, ScriptedProvider())


def release(c, agent_id, version):
    assert op_done(c.post(f"/api/agents/{agent_id}/versions/{version}/test"))["passed"]
    ok(c.post(f"/api/agents/{agent_id}/versions/{version}/approve", json={"note": "ok"}))
    ok(c.post(f"/api/agents/{agent_id}/deploy", json={"version": version}))


def version_info(c, agent_id, version):
    return ok(c.get(f"/api/agents/{agent_id}/versions/{version}"))


# ---------------------------------------------------------------- test cases

def test_test_cases_are_added_and_removed_as_new_versions(env):
    c = env.client()
    signup(c)
    agent_id, v1 = build_agent(c)
    v2 = ok(c.post(f"/api/agents/{agent_id}/versions/{v1}/test-cases", json={"cases": [NEW_CASE]}))["version"]
    assert v2 == v1 + 1
    info = version_info(c, agent_id, v2)
    assert info["origin"] == "tests_added" and "orders this week" in info["version_note"]
    assert info["status"] == "draft"  # a changed test suite must be run again
    assert "orders this week" in {t["name"] for t in info["spec"]["test_cases"]}
    assert "orders this week" not in {t["name"] for t in version_info(c, agent_id, v1)["spec"]["test_cases"]}

    assert c.post(f"/api/agents/{agent_id}/versions/{v2}/test-cases", json={"cases": [NEW_CASE]}).status_code == 409
    bad_tool = {**NEW_CASE, "name": "x", "must_call": ["no_such_tool"]}
    assert c.post(f"/api/agents/{agent_id}/versions/{v2}/test-cases", json={"cases": [bad_tool]}).status_code == 422
    assert c.post(f"/api/agents/{agent_id}/versions/{v2}/test-cases",
                  json={"cases": [{**NEW_CASE, "category": "made_up"}]}).status_code == 422

    v3 = ok(c.delete(f"/api/agents/{agent_id}/versions/{v2}/test-cases/orders this week"))["version"]
    assert "orders this week" not in {t["name"] for t in version_info(c, agent_id, v3)["spec"]["test_cases"]}
    assert version_info(c, agent_id, v3)["origin"] == "tests_removed"
    assert c.delete(f"/api/agents/{agent_id}/versions/{v3}/test-cases/nope").status_code == 404


def test_suggested_test_cases_are_labelled_and_only_added_when_chosen(env):
    c = env.client()
    signup(c)
    agent_id, v1 = build_agent(c)
    result = op_done(c.post(f"/api/agents/{agent_id}/versions/{v1}/suggest-tests"))
    assert result["generated_by"] == "scripted (simulated)"
    [suggested] = result["cases"]
    assert suggested["name"] == "region count" and suggested["must_include"] == ["4"]
    assert ok(c.get(f"/api/agents/{agent_id}"))["version"] == v1  # nothing changes until a person adds it
    v2 = ok(c.post(f"/api/agents/{agent_id}/versions/{v1}/test-cases", json={"cases": [suggested]}))["version"]
    report = op_done(c.post(f"/api/agents/{agent_id}/versions/{v2}/test"))
    tests = ok(c.get(f"/api/agents/{agent_id}/versions/{v2}/tests"))[0]["report"]
    case = next(x for x in tests["cases"] if x["name"] == "region count")
    assert case["passed"] and report["passed"]
    assert tests["simulated"] is True and tests["provider"] == "scripted"


# ---------------------------------------------------------------- diagnose, fix, retest

def test_failed_run_is_diagnosed_fixed_and_retested(tmp_path):
    env = Env(tmp_path, ScriptedProvider())
    c = env.client()
    signup(c)
    agent_id, v1 = build_agent(c)
    env.model.fail_agent_runs = True
    op = ok(c.post(f"/api/agents/{agent_id}/runs", json={"task": "What is total revenue?", "mode": "sandbox"}))
    run_id = op["result"]["run_id"]
    env.model.fail_agent_runs = False

    findings = ok(c.get(f"/api/runs/{run_id}/diagnosis"))
    assert findings and findings[0]["severity"] == "error"
    assert "simulated model outage" in findings[0]["detail"] and findings[0]["suggestion"]

    fix = op_done(c.post(f"/api/agents/{agent_id}/versions/{v1}/propose-fix",
                         json={"run_id": run_id, "note": "It should always give the total."}))
    assert fix["generated_by"] == "scripted (simulated)" and fix["version"] == v1
    evidence = env.model.requests[-1]["prompt"]
    assert f"Run #{run_id}" in evidence and "simulated model outage" in evidence
    assert "It should always give the total." in evidence

    v2 = ok(c.post(f"/api/agents/{agent_id}/versions/{v1}/apply-fix", json={"proposal": fix["proposal"]}))["version"]
    info = version_info(c, agent_id, v2)
    assert info["origin"] == "fixed" and info["status"] == "draft" and info["spec"]["limits"]["max_turns"] == 12
    regression = next(t for t in info["spec"]["test_cases"] if t["name"] == "total is calculated")
    assert regression["must_include"] == ["20651"]
    assert c.post(f"/api/agents/{agent_id}/deploy", json={"version": v2}).status_code == 400  # untested

    report = op_done(c.post(f"/api/agents/{agent_id}/versions/{v2}/test"))
    assert report["passed"]
    case = next(x for x in ok(c.get(f"/api/agents/{agent_id}/versions/{v2}/tests"))[0]["report"]["cases"]
                if x["name"] == "total is calculated")
    assert case["passed"] and "20651" in case["transcript"]


def test_failed_test_cases_carry_a_diagnosis_and_can_be_fixed(tmp_path):
    env = Env(tmp_path, ScriptedProvider())
    c = env.client()
    signup(c)
    agent_id, v1 = build_agent(c)
    env.model.send_after_query = True  # the agent now tries to send the report on every task
    report = op_done(c.post(f"/api/agents/{agent_id}/versions/{v1}/test"))
    assert not report["passed"]
    cases = ok(c.get(f"/api/agents/{agent_id}/versions/{v1}/tests"))[0]["report"]["cases"]
    failed = [x for x in cases if not x["passed"]]
    assert failed and all(x["diagnosis"] for x in failed)
    assert c.post(f"/api/agents/{agent_id}/versions/{v1}/approve", json={}).status_code == 400

    fix = op_done(c.post(f"/api/agents/{agent_id}/versions/{v1}/propose-fix", json={"case_name": failed[0]["name"]}))
    assert f"Failed test case {failed[0]['name']}" in env.model.requests[-1]["prompt"]
    assert fix["proposal"]["regression_test"]["name"]
    assert c.post(f"/api/agents/{agent_id}/versions/{v1}/propose-fix", json={}).status_code == 422
    assert c.post(f"/api/agents/{agent_id}/versions/{v1}/propose-fix",
                  json={"case_name": "no such case"}).status_code == 404


# ---------------------------------------------------------------- versions and restore

def test_restoring_an_earlier_version_makes_a_draft_that_must_be_retested(env):
    c = env.client()
    signup(c)
    agent_id, v1 = build_agent(c)
    release(c, agent_id, v1)
    v2 = ok(c.put(f"/api/agents/{agent_id}/versions/{v1}/config", json={"max_turns": 3}))["version"]
    release(c, agent_id, v2)
    assert ok(c.get(f"/api/agents/{agent_id}"))["agent"]["deployed_version"] == v2

    # roll back the deployment to the earlier approved version directly
    ok(c.post(f"/api/agents/{agent_id}/deploy", json={"version": v1}))
    assert ok(c.get(f"/api/agents/{agent_id}"))["agent"]["deployed_version"] == v1

    # or restore it as a new draft to build on
    v3 = ok(c.post(f"/api/agents/{agent_id}/versions/{v1}/restore"))["version"]
    info = version_info(c, agent_id, v3)
    assert info["origin"] == "restored" and f"Restored from v{v1}" in info["version_note"]
    assert info["status"] == "draft" and not info["can_deploy"] and info["approval"] is None
    assert info["spec"]["limits"] == version_info(c, agent_id, v1)["spec"]["limits"]
    assert c.post(f"/api/agents/{agent_id}/deploy", json={"version": v3}).status_code == 400
    assert ok(c.get(f"/api/agents/{agent_id}"))["agent"]["deployed_version"] == v1  # untouched
    versions = {v["version"]: v for v in ok(c.get(f"/api/agents/{agent_id}"))["versions"]}
    assert versions[v3]["origin"] == "restored" and versions[v1]["origin"] == "generated"
    assert versions[v1]["provider"] == "scripted (simulated)"


def test_versions_of_other_organisations_cannot_be_changed(env):
    a = env.client()
    signup(a)
    agent_id, v1 = build_agent(a)
    b = env.client()
    signup(b, org="Competitor AB", email="eve@competitor.se", name="Eve")
    for method, path, body in [
        ("post", f"/api/agents/{agent_id}/versions/{v1}/test-cases", {"cases": [NEW_CASE]}),
        ("delete", f"/api/agents/{agent_id}/versions/{v1}/test-cases/total check", None),
        ("post", f"/api/agents/{agent_id}/versions/{v1}/suggest-tests", None),
        ("post", f"/api/agents/{agent_id}/versions/{v1}/propose-fix", {"note": "x"}),
        ("post", f"/api/agents/{agent_id}/versions/{v1}/restore", None),
    ]:
        r = b.request(method.upper(), path, json=body)
        assert r.status_code == 404, path
    run_id = op_done(a.post(f"/api/agents/{agent_id}/runs", json={"task": "revenue?", "mode": "sandbox"}))["run_id"]
    assert b.get(f"/api/runs/{run_id}/diagnosis").status_code == 404


# ---------------------------------------------------------------- simulated vs real

def test_test_mode_is_visible_everywhere(env):
    c = env.client()
    signup(c)
    assert ok(c.get("/api/me"))["provider"] == {"name": "scripted", "simulated": True}
    agent_id, v1 = build_agent(c)
    run_id = op_done(c.post(f"/api/agents/{agent_id}/runs", json={"task": "revenue?", "mode": "sandbox"}))["run_id"]
    run = ok(c.get(f"/api/runs/{run_id}"))
    assert run["simulated"] is True and run["provider"] == "scripted"
    assert ok(c.get("/api/runs"))[0]["simulated"] is True
    usage = ok(c.get("/api/monitoring"))["usage"]
    assert usage and all(u["simulated"] and u["provider"] == "scripted" for u in usage)
    settings = ok(c.get("/api/settings"))
    assert settings["model"]["simulated"] is True and settings["month_spend_real_usd"] == 0
    assert settings["month_spend_simulated_usd"] > 0
    check = ok(c.post("/api/settings/check-model"))
    assert check["ok"] and check["provider"] == "scripted"


def test_simulated_results_cannot_release_a_version_on_a_real_model_server(tmp_path, monkeypatch):
    env = Env(tmp_path, ScriptedProvider())
    c = env.client()
    signup(c)
    agent_id, v1 = build_agent(c)
    assert op_done(c.post(f"/api/agents/{agent_id}/versions/{v1}/test"))["passed"]

    # the same database, now served with the real provider (no key here, so no call can happen)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    real = TestClient(create_app(str(tmp_path / "web.db"), start_worker=False, inline_operations=True))
    real.headers.update(H)
    ok(real.post("/api/auth/login", json={"email": "karin@nordhem.se", "password": "correct horse battery"}))
    assert ok(real.get("/api/me"))["provider"] == {"name": "anthropic", "simulated": False}
    info = version_info(real, agent_id, v1)
    assert "scripted test model" in info["simulated_results_block"]
    assert not info["can_approve"] and not info["can_deploy"]
    r = real.post(f"/api/agents/{agent_id}/versions/{v1}/approve", json={})
    assert r.status_code == 400 and "scripted test model" in r.json()["detail"]
    r = real.post(f"/api/agents/{agent_id}/deploy", json={"version": v1})
    assert r.status_code == 400 and "scripted test model" in r.json()["detail"]
    # simulated spend never counts against the real budget
    s = ok(real.get("/api/settings"))
    assert s["month_spend_usd"] == 0 and s["month_spend_simulated_usd"] > 0
    # and the model connection check reports the missing key without calling anything
    check = ok(real.post("/api/settings/check-model"))
    assert not check["ok"] and "ANTHROPIC_API_KEY" in check["message"]


# ---------------------------------------------------------------- errors and duplicates

def test_model_errors_reach_the_user_as_actionable_messages(tmp_path):
    class RateLimited(ScriptedProvider):
        def structured(self, *a, **kw):
            raise ModelRateLimited("The model service is rate limiting requests.", retry_after=30)

    env = Env(tmp_path, RateLimited())
    c = env.client()
    signup(c)
    pid = ok(c.post("/api/projects", json={
        "title": "Reports", "company": "Nordhem Design", "industry": "Retail",
        "problem": "We spend hours every week building the same sales report by hand from exports.",
        "workflow": "", "outcome": "", "systems": "", "constraints": ""}))["id"]
    op = ok(c.post(f"/api/projects/{pid}/questions"))
    assert op["status"] == "failed"
    assert "rate limiting" in op["error"] and "Traceback" not in op["error"]
    assert op["result"] == {"error_kind": "ModelRateLimited", "retryable": True}


def test_an_approved_action_runs_once_even_if_approved_twice(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    cid = store.get_or_create_customer("Nordhem Design")
    store.save_version(cid, load_spec(SALES / "spec.yaml"))
    agent_id = store.agent("weekly-sales-insights")["id"]
    a = store.queue_action(agent_id, None, "send_report", {"to": "x@example.com"}, idem_key="k1")
    assert store.queue_action(agent_id, None, "send_report", {"to": "x@example.com"}, idem_key="k1") == a
    assert store.claim_action(a, "Karin") is True
    assert store.claim_action(a, "Olle") is False  # the second approver loses; nothing runs twice
