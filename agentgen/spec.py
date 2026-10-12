"""The agent specification: a declarative, versioned definition of an agent.

An agent is data, not code. The runtime executes a spec using capabilities from the
component library, so new agent types come from new combinations and configurations.

`AgentDesign` is what the designer model produces. Every field is required so it can be
used directly as a structured-output schema. `AgentSpec` adds deployment settings that a
human controls. Specs stored by earlier versions are upgraded by `upgrade_spec_dict`.
"""

from typing import Literal

import yaml
from pydantic import BaseModel, Field

from .llm import DEFAULT_MODEL

Risk = Literal["read", "write_internal", "write_external", "external_comm", "financial"]
RISK_ORDER = ["read", "write_internal", "write_external", "external_comm", "financial"]

TestCategory = Literal[
    "task", "quality", "tool_use", "missing_info", "permission", "unauthorized_action", "api_failure", "edge_case"
]
SAFETY_CATEGORIES = {"permission", "unauthorized_action"}


class ConfigItem(BaseModel):
    key: str
    value: str


class ToolSpec(BaseModel):
    name: str = Field(description="Tool name the agent sees: snake_case, unique within the agent.")
    component: str = Field(description="Name of a component from the catalog.")
    purpose: str = Field(description="Why this agent needs the tool and when to use it.")
    requires_approval: bool = Field(description="True if a human must approve each call.")
    config: list[ConfigItem] = Field(description="Component settings, e.g. record_type=case.")

    def config_dict(self) -> dict[str, str]:
        return {item.key: item.value for item in self.config}


class DataSource(BaseModel):
    name: str
    kind: Literal["dataset", "documents", "api", "other"]
    description: str
    status: Literal["available", "needs_customer", "needs_connector"] = Field(
        description="available: usable now; needs_customer: customer must provide data or credentials; "
        "needs_connector: no component can reach it yet."
    )


class Trigger(BaseModel):
    kind: Literal["manual", "schedule", "event"]
    description: str
    interval_minutes: int = Field(description="Minutes between scheduled runs; 0 unless kind is schedule.")
    event: str = Field(description="Event name, e.g. new_request; empty unless kind is event.")
    input_template: str = Field(
        description="Message the agent receives when triggered. For events, {payload} is replaced by the event data."
    )


class WorkflowStep(BaseModel):
    step: str
    description: str
    on_failure: str = Field(description="What to do if this step can't be completed.")


class MemoryPolicy(BaseModel):
    enabled: bool
    what_to_remember: str = Field(description="What may be stored between runs; empty if disabled.")
    retention_days: int


class Limits(BaseModel):
    max_turns: int = Field(description="Maximum model turns per run, typically 8-25.")
    max_tokens_per_run: int = Field(description="Token budget per run (input + output), typically 50000-400000.")


class Fixture(BaseModel):
    tool: str
    response: str = Field(description="What the tool returns in this test instead of calling a real system.")


class TestCase(BaseModel):
    __test__ = False  # not a pytest test class

    name: str
    category: TestCategory
    input: str = Field(description="The message, event payload or task the agent receives.")
    expected_behavior: str = Field(description="What a correct response does, in plain language.")
    must_call: list[str] = Field(description="Tool names the agent must use.")
    must_not_call: list[str] = Field(description="Tool names the agent must not use.")
    fail_tools: list[str] = Field(description="Tool names that fail in this test (simulated outage).")
    fixtures: list[Fixture] = Field(description="Responses for tools that would reach external systems.")


class AgentDesign(BaseModel):
    name: str = Field(description="Short lowercase slug, e.g. service-request-router.")
    purpose: str = Field(description="One or two sentences on what the agent does and for whom.")
    capabilities: list[str] = Field(description="What the agent can do, e.g. 'look up tenants in the database'.")
    instructions: str = Field(description="The agent's full operating instructions, written to the agent.")
    workflow: list[WorkflowStep]
    tools: list[ToolSpec]
    data_sources: list[DataSource]
    triggers: list[Trigger]
    memory: MemoryPolicy
    limits: Limits
    success_criteria: list[str]
    restrictions: list[str] = Field(description="Things the agent must never do.")
    test_cases: list[TestCase]
    assumptions: list[str] = Field(description="Assumptions made where the requirements were silent.")
    open_questions: list[str] = Field(description="Information or integrations still missing.")


class AgentSpec(AgentDesign):
    model: str = DEFAULT_MODEL
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"

    def tool(self, name: str) -> ToolSpec | None:
        return next((t for t in self.tools if t.name == name), None)

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.model_dump(), sort_keys=False, allow_unicode=True, width=100)

    @classmethod
    def from_yaml(cls, text: str) -> "AgentSpec":
        return cls.model_validate(upgrade_spec_dict(yaml.safe_load(text)))

    @classmethod
    def from_json(cls, text: str) -> "AgentSpec":
        return cls.model_validate(upgrade_spec_dict(yaml.safe_load(text)))  # JSON is valid YAML


def upgrade_spec_dict(data: dict) -> dict:
    """Fill fields that version 1 specs didn't have, so stored agents keep working."""
    data = dict(data)
    data.setdefault("capabilities", [])
    data.setdefault("workflow", [])
    data.setdefault("data_sources", [])
    data.setdefault("triggers", [{"kind": "manual", "description": "Run on request", "interval_minutes": 0,
                                  "event": "", "input_template": ""}])
    data.setdefault("memory", {"enabled": False, "what_to_remember": "", "retention_days": 0})
    data.setdefault("limits", {"max_turns": 20, "max_tokens_per_run": 200000})
    data.setdefault("assumptions", [])
    renamed = {"draft_email": "draft_message", "send_email": "send_message"}
    tools = []
    for t in data.get("tools", []):
        t = dict(t)
        t["component"] = renamed.get(t["component"], t["component"])
        t.setdefault("name", t["component"])
        t.setdefault("purpose", "")
        t.setdefault("config", [])
        if t["component"] == "send_message" and not any(c["key"] == "channel" for c in t["config"]):
            t["config"] = t["config"] + [{"key": "channel", "value": "email"}]
        tools.append(t)
    data["tools"] = tools
    cases = []
    for c in data.get("test_cases", []):
        c = dict(c)
        c.setdefault("category", "task")
        c.setdefault("fail_tools", [])
        c.setdefault("fixtures", [])
        c["must_call"] = [renamed.get(n, n) for n in c.get("must_call", [])]
        c["must_not_call"] = [renamed.get(n, n) for n in c.get("must_not_call", [])]
        cases.append(c)
    data["test_cases"] = cases
    return data
