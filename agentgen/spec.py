"""The agent specification: the structured contract between requirements and a running agent.

`AgentDesign` is what the designer model produces (every field required, so it can be
used directly as a structured-output schema). `AgentSpec` adds deployment settings that
a human controls rather than the model.
"""

from typing import Literal

import yaml
from pydantic import BaseModel, Field

from .llm import DEFAULT_MODEL


class ConfigItem(BaseModel):
    key: str
    value: str


class ToolSpec(BaseModel):
    component: str = Field(description="Name of a component from the catalog.")
    requires_approval: bool = Field(description="True if a human must approve each call.")
    config: list[ConfigItem] = Field(description="Component settings, e.g. table=invoices.")

    def config_dict(self) -> dict[str, str]:
        return {item.key: item.value for item in self.config}


class TestCase(BaseModel):
    name: str
    input: str = Field(description="The message or task the agent receives.")
    expected_behavior: str = Field(description="What a correct response does, in plain language.")
    must_call: list[str] = Field(description="Component names the agent must use.")
    must_not_call: list[str] = Field(description="Component names the agent must not use.")


class AgentDesign(BaseModel):
    name: str = Field(description="Short lowercase slug, e.g. client-email-triage.")
    purpose: str = Field(description="One or two sentences on what the agent does and for whom.")
    instructions: str = Field(description="The agent's full operating instructions, written to the agent.")
    tools: list[ToolSpec]
    success_criteria: list[str]
    restrictions: list[str] = Field(description="Things the agent must never do.")
    test_cases: list[TestCase]
    open_questions: list[str] = Field(description="Information missing from the requirements.")


class AgentSpec(AgentDesign):
    model: str = DEFAULT_MODEL
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"

    def tool(self, component: str) -> ToolSpec | None:
        return next((t for t in self.tools if t.component == component), None)

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.model_dump(), sort_keys=False, allow_unicode=True, width=100)

    @classmethod
    def from_yaml(cls, text: str) -> "AgentSpec":
        return cls.model_validate(yaml.safe_load(text))
