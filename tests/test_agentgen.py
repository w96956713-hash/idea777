"""Offline tests: a scripted fake client stands in for the Claude API."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentgen.cli import main
from agentgen.designer import validate_design
from agentgen.evaluate import check_tools, evaluate
from agentgen.runtime import AgentRuntime
from agentgen.spec import AgentSpec, ToolSpec
from agentgen.spec import TestCase as Case
from agentgen.store import Store

EXAMPLE = Path(__file__).parent.parent / "examples" / "accounting_firm"


def text(t):
    return SimpleNamespace(type="text", text=t)


def tool_use(name, args, id="t1"):
    return SimpleNamespace(type="tool_use", name=name, input=args, id=id)


def response(*blocks, stop="end_turn"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop,
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5))


class FakeClient:
    """Returns scripted agent responses from create() and judge verdicts from parse()."""

    def __init__(self, responses, verdict=True):
        self.responses = list(responses)
        self.verdict = verdict
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create, parse=self._parse))

    def _create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)

    def _parse(self, **kwargs):
        verdict = kwargs["output_format"](reasoning="ok", passed=self.verdict)
        return SimpleNamespace(parsed_output=verdict, stop_reason="end_turn")


@pytest.fixture
def store():
    s = Store(":memory:")
    spec = AgentSpec.from_yaml((EXAMPLE / "spec.yaml").read_text(encoding="utf-8"))
    s.save_version(s.get_or_create_customer("Andersson"), spec)
    agent_id = s.agent(spec.name)["id"]
    for f in (EXAMPLE / "knowledge").glob("*.md"):
        s.add_knowledge(agent_id, f.stem, f.read_text(encoding="utf-8"))
    return s


def runtime(store, client, approver=lambda *_: True):
    spec, version = store.load_spec("client-email-triage")
    return AgentRuntime(client, store, store.agent(spec.name)["id"], spec, version, approver)


def test_tool_loop_runs_tools_and_logs(store):
    client = FakeClient([
        response(tool_use("search_knowledge", {"query": "bokföring enskild firma pris"}), stop="tool_use"),
        response(text("Från 1 450 kr/månad.")),
    ])
    result = runtime(store, client).run("Vad kostar bokföring?")
    assert result.text == "Från 1 450 kr/månad."
    assert "1 450" in result.tool_calls[0].result  # knowledge search found the price list
    # tool result is sent back to the model in the next request
    assert client.requests[1]["messages"][-1]["content"][0]["type"] == "tool_result"
    events = [r["event"] for r in store.conn.execute("SELECT event FROM audit_log")]
    assert events == ["run_started", "tool_call", "run_finished"]
    assert store.usage_summary("client-email-triage")["input_tokens"] == 20


def test_denied_approval_blocks_action(store):
    client = FakeClient([
        response(tool_use("send_email", {"to": "a@b.se", "subject": "s", "body": "b"}), stop="tool_use"),
        response(text("Escalating instead.")),
    ])
    result = runtime(store, client, approver=lambda *_: False).run("hi")
    assert result.tool_calls[0].approved is False
    assert "send_email" not in result.called
    assert store.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_tool_not_in_spec_is_blocked(store):
    client = FakeClient([
        response(tool_use("draft_email", {"to": "a@b.se", "subject": "s", "body": "b"}), stop="tool_use"),
        response(text("done")),
    ])
    result = runtime(store, client).run("hi")
    assert "not available" in result.tool_calls[0].result


def test_send_email_respects_allowed_domains(store):
    spec, _ = store.load_spec("client-email-triage")
    spec.tool("send_email").config[0].value = "kund.se"
    client = FakeClient([
        response(tool_use("send_email", {"to": "x@evil.se", "subject": "s", "body": "b"}), stop="tool_use"),
        response(text("done")),
    ])
    rt = AgentRuntime(client, store, store.agent(spec.name)["id"], spec, 1, lambda *_: True)
    assert rt.run("hi").tool_calls[0].result.startswith("Refused")


def test_check_tools():
    case = Case(name="c", input="i", expected_behavior="e", must_call=["a"], must_not_call=["b"])
    assert check_tools(case, {"a"}) == ""
    assert "did not call a" in check_tools(case, set())
    assert "forbidden b" in check_tools(case, {"a", "b"})


def test_evaluate_uses_sandbox_and_gates_deploy(store):
    spec, _ = store.load_spec("client-email-triage")
    spec.test_cases = spec.test_cases[:1]  # price question: must call search_knowledge + send_email
    store.save_version(store.get_or_create_customer("Andersson"), spec)  # v2
    client = FakeClient([
        response(tool_use("search_knowledge", {"query": "pris"}), stop="tool_use"),
        response(tool_use("send_email", {"to": "lisa@lisasfrisor.se", "subject": "Pris", "body": "1 450 kr"},
                          id="t2"), stop="tool_use"),
        response(text("Svar skickat.")),
    ])
    with pytest.raises(ValueError):
        store.deploy("client-email-triage", 2)  # untested versions can't deploy
    report = evaluate(client, store, "client-email-triage", 2)
    assert report.passed and report.pass_rate == 1.0
    # the test's email went to the sandbox, not the real outbox
    assert store.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    store.deploy("client-email-triage", 2)
    assert store.deployed_spec("client-email-triage")[1] == 2


def test_failed_tool_check_fails_case_even_if_judge_passes(store):
    spec, _ = store.load_spec("client-email-triage")
    spec.test_cases = spec.test_cases[:1]
    store.save_version(store.get_or_create_customer("Andersson"), spec)
    client = FakeClient([response(text("Det kostar ungefär 1000 kr."))], verdict=True)
    report = evaluate(client, store, "client-email-triage", 2)
    assert not report.passed
    assert store.version_status("client-email-triage", 2) == "failed"


def test_validate_design_removes_unknown_and_adds_escalation():
    spec = AgentSpec(
        name="x", purpose="p", instructions="i", success_criteria=[], restrictions=[], open_questions=[],
        tools=[ToolSpec(component="teleport", requires_approval=False, config=[]),
               ToolSpec(component="search_knowledge", requires_approval=False, config=[])],
        test_cases=[Case(name="c", input="i", expected_behavior="e", must_call=["teleport", "search_knowledge"],
                             must_not_call=[])],
    )
    spec = validate_design(spec)
    assert [t.component for t in spec.tools] == ["search_knowledge", "escalate_to_human"]
    assert spec.test_cases[0].must_call == ["search_knowledge"]
    assert any("teleport" in q for q in spec.open_questions)


def test_agents_are_isolated_per_customer(store):
    spec, _ = store.load_spec("client-email-triage")
    with pytest.raises(ValueError):
        store.save_version(store.get_or_create_customer("Other AB"), spec)


def test_cli_import_export_roundtrip(tmp_path, capsys):
    db = str(tmp_path / "t.db")
    main(["--db", db, "import", str(EXAMPLE / "spec.yaml"), "--customer", "Andersson"])
    main(["--db", db, "knowledge", "client-email-triage", *map(str, (EXAMPLE / "knowledge").glob("*.md"))])
    out = tmp_path / "out.yaml"
    main(["--db", db, "export", "client-email-triage", "-o", str(out)])
    assert AgentSpec.from_yaml(out.read_text(encoding="utf-8")).name == "client-email-triage"
    main(["--db", db, "list"])
    assert "client-email-triage" in capsys.readouterr().out
