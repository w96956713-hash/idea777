"""Offline tests of the `agentgen live-eval` command. No real API call is made: without a key the
command refuses to start, and the flow itself is exercised with the scripted provider (whose
results are labelled simulated) and with the Anthropic provider over mocked HTTP."""

import json

import pytest

from agentgen import cli
from agentgen.live_eval import run_live_eval
from agentgen.providers import ModelNotConfigured
from fake_model import ScriptedProvider
from test_provider import Recorder, error_response, provider


def test_refuses_to_start_without_a_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ModelNotConfigured):
        run_live_eval(workdir=tmp_path / "eval")
    with pytest.raises(SystemExit) as e:
        cli.main(["live-eval", "--dir", str(tmp_path / "eval")])
    assert "ANTHROPIC_API_KEY" in str(e.value)
    assert not (tmp_path / "eval").exists()  # nothing was set up


def test_stops_at_a_failed_connection_check_without_spending(tmp_path):
    rec = Recorder(error_response(401, "invalid x-api-key", None, "authentication_error"))
    result = run_live_eval(workdir=tmp_path / "eval", provider=provider(rec), out=lambda *a: None)
    assert result["ok"] is False and result["stage"] == "connection"
    assert "ANTHROPIC_API_KEY" in result["message"]
    assert [r.url.path for r in rec.requests] == ["/v1/models/claude-opus-5-5"]  # no message requests
    assert not (tmp_path / "eval").exists()


def test_runs_the_example_suite_with_exact_checks_and_labels_simulated_results(tmp_path):
    lines = []
    result = run_live_eval(budget_usd=5, workdir=tmp_path / "eval", provider=ScriptedProvider(), out=lines.append)
    assert result["ok"] and result["simulated"] is True and result["provider"] == "scripted"
    cases = {c["name"]: c for c in result["report"]["cases"]}
    assert len(cases) == 7
    assert cases["revenue by region"]["tool_check"] == "ok"  # includes the exact figures 10325, 3294, 2297, 4735
    assert 0 < result["estimated_cost_usd"] <= 5
    assert any("simulated provider was used" in line for line in lines)
    saved = json.loads((tmp_path / "eval" / "live_eval_report.json").read_text())
    assert saved["simulated"] is True and saved["report"]["agent"] == "weekly-sales-insights"


def test_exact_figures_are_enforced(tmp_path):
    class WrongFigures(ScriptedProvider):
        def _decide(self, tool_list, messages):
            reply = super()._decide(tool_list, messages)
            for block in reply.content:
                if block.type == "text":
                    block.text = block.text.replace("10325", "10300")
            return reply

    result = run_live_eval(budget_usd=5, workdir=tmp_path / "eval", provider=WrongFigures(), out=lambda *a: None)
    region = next(c for c in result["report"]["cases"] if c["name"] == "revenue by region")
    assert not region["passed"] and "missing '10325'" in region["tool_check"]


def test_spending_cap_is_hard(tmp_path):
    result = run_live_eval(budget_usd=0.05, workdir=tmp_path / "eval", provider=ScriptedProvider(),
                           out=lambda *a: None)
    assert result["estimated_cost_usd"] <= 0.05
    assert not result["passed"]
    errors = [c["error"] for c in result["report"]["cases"] if c["error"]]
    assert errors and "budget" in errors[-1].lower()
    assert len(result["report"]["cases"]) < 7  # stopped instead of failing every remaining case
