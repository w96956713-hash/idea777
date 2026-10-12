"""Shared tools for the offline evaluation suite (tests/test_eval_*.py).

The suite checks how the PLATFORM behaves in realistic scenarios - permissions, approvals, data
handling, failures, calculations, regressions - with deterministic assertions. The model is a
`PlaybookProvider` that follows a fixed script of actions, so every run is repeatable. These
results say nothing about how well a real model performs; that needs the live evaluation
(docs/DEPLOYMENT.md, "First real-model evaluation").
"""

from pathlib import Path
from types import SimpleNamespace

from agentgen import datasets
from agentgen.designer import validate_design
from agentgen.evaluate import Verdict
from agentgen.providers import ModelProvider, ProviderStatus, Structured, ToolUse, Turn, Usage
from agentgen.spec import AgentSpec, TestCase
from agentgen.store import Store

ROOT = Path(__file__).parent.parent
EXAMPLES = ROOT / "examples"
SALES = EXAMPLES / "sales_insights"
PROPERTY = EXAMPLES / "property_requests"


# ---------------------------------------------------------------- playbook model

def tool(name, **args):
    """Playbook step: the model calls a tool."""
    return ("tool", name, args)


def tools(*calls):
    """Playbook step: the model calls several tools in one turn. calls: (name, args) pairs."""
    return ("tools", calls)


def say(text):
    """Playbook step: the model finishes with this text."""
    return ("text", text)


def stop(reason, text="", calls=()):
    """Playbook step: a turn that ends with an unusual stop reason (max_tokens, refusal, tool_use...)."""
    return ("stop", reason, text, calls)


def fail(error):
    """Playbook step: the model call raises this error (e.g. ModelRateLimited(...))."""
    return ("error", error)


def raw_input(name, value):
    """Playbook step: a tool call whose input is not a normal dict (malformed model output)."""
    return ("raw", name, value)


class PlaybookProvider(ModelProvider):
    """Follows `steps` in order, one per agent turn. Structured calls return `outputs[type]`, and test
    verdicts come from `verdict` (default: pass). Labelled simulated. `turns` records every request."""

    name = "playbook"
    simulated = True

    def __init__(self, steps=(), outputs=None, verdict=True, usage=(1000, 100)):
        self.steps = list(steps)
        self.outputs = dict(outputs or {})
        self.verdict = verdict
        self.usage = usage
        self.turns = []
        self.structured_calls = []
        self._n = 0

    def check(self, model):
        return ProviderStatus(True, self.name, model, "Playbook (simulated).")

    def structured(self, output_type, *, system, prompt, model, effort="high", max_tokens=32000):
        self.structured_calls.append({"type": output_type.__name__, "prompt": prompt})
        if output_type is Verdict and output_type not in self.outputs:
            v = self.verdict(prompt) if callable(self.verdict) else self.verdict
            value = v if isinstance(v, Verdict) else Verdict(reasoning="playbook verdict", passed=bool(v))
        else:
            value = self.outputs[output_type]
            if isinstance(value, Exception):
                raise value
        return Structured(value=value, usage=Usage(*self.usage), model=model)

    def turn(self, *, model, system, tools, messages, effort="medium", max_tokens=16000):
        self.turns.append({"system": system, "tools": tools, "messages": list(messages)})
        if not self.steps:
            return self._turn("end_turn", "Done.", [])
        step = self.steps.pop(0)
        kind = step[0]
        if kind == "error":
            raise step[1]
        if kind == "text":
            return self._turn("end_turn", step[1], [])
        if kind == "tool":
            return self._turn("tool_use", "", [(step[1], step[2])])
        if kind == "tools":
            return self._turn("tool_use", "", list(step[1]))
        if kind == "raw":
            return self._turn("tool_use", "", [(step[1], step[2])])
        if kind == "stop":
            return self._turn(step[1], step[2], list(step[3]))
        raise ValueError(f"unknown playbook step {step!r}")

    def _turn(self, stop_reason, text, calls):
        content = []
        if text:
            content.append(SimpleNamespace(type="text", text=text))
        uses = []
        for name, args in calls:
            self._n += 1
            use = ToolUse(id=f"toolu_{self._n}", name=name, input=args)
            uses.append(use)
            content.append(SimpleNamespace(type="tool_use", id=use.id, name=name, input=args))
        return Turn(stop_reason=stop_reason, text=text, tool_uses=uses, content=content, usage=Usage(*self.usage),
                    model="playbook")


# ---------------------------------------------------------------- environments

def load_spec(path: Path) -> AgentSpec:
    return validate_design(AgentSpec.from_yaml(path.read_text(encoding="utf-8")))


def sales_env(tmp_path, customer="Nordhem Design") -> tuple[Store, AgentSpec, int]:
    """A store with the weekly-sales-insights agent (v1) and its orders/products data."""
    store = Store(str(tmp_path / "eval.db"))
    cid = store.get_or_create_customer(customer)
    datasets.import_csv(store, cid, "orders", SALES / "data" / "orders.csv")
    datasets.import_csv(store, cid, "products", SALES / "data" / "products.csv")
    spec = load_spec(SALES / "spec.yaml")
    store.save_version(cid, spec, origin="imported")
    return store, spec, cid


def property_env(tmp_path, customer="Hemvist Fastigheter AB") -> tuple[Store, AgentSpec, int]:
    """A store with the service-request-router agent (v1), tenants/properties data and routing policy."""
    store = Store(str(tmp_path / "eval.db"))
    cid = store.get_or_create_customer(customer)
    datasets.import_csv(store, cid, "tenants", PROPERTY / "data" / "tenants.csv")
    datasets.import_csv(store, cid, "properties", PROPERTY / "data" / "properties.csv")
    spec = load_spec(PROPERTY / "spec.yaml")
    store.save_version(cid, spec, origin="imported")
    store.add_knowledge(store.agent(spec.name)["id"], "routing_policy",
                        (PROPERTY / "knowledge" / "routing_policy.md").read_text(encoding="utf-8"))
    return store, spec, cid


def case(name, category="task", input="x", expected="y", must_call=(), must_not_call=(), fail_tools=(),
         fixtures=(), must_include=(), must_not_include=()) -> TestCase:
    return TestCase(name=name, category=category, input=input, expected_behavior=expected,
                    must_call=list(must_call), must_not_call=list(must_not_call), fail_tools=list(fail_tools),
                    fixtures=[{"tool": t, "response": r} for t, r in fixtures], must_include=list(must_include),
                    must_not_include=list(must_not_include))


def mark_passed(store: Store, agent_name: str, version: int) -> None:
    """Record a passing test report (with safety cases) without running tests - for scenarios that
    exercise deployment and live runs."""
    store.record_test_run(agent_name, version, 1.0, True, {"cases": [], "passed": True, "has_safety_cases": True})


def outbox(store: Store) -> list[dict]:
    return [dict(r) for r in store.conn.execute("SELECT * FROM outbox ORDER BY id")]


def records(store: Store, record_type: str | None = None) -> list[dict]:
    sql, params = "SELECT * FROM records", ()
    if record_type:
        sql, params = sql + " WHERE table_name = ?", (record_type,)
    return [dict(r) for r in store.conn.execute(sql + " ORDER BY id", params)]


def events(store: Store, run_id: int) -> list[str]:
    return [r["event"] for r in store.run_events(run_id)]
