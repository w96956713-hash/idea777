"""The model-provider interface the engine depends on.

The engine (pipeline, runtime, evaluation) only ever talks to a `ModelProvider`. Swapping the
model vendor, or using a scripted test double, means supplying another implementation; the
engine does not change.

Two operations cover everything the engine needs:
- `structured(...)`: one request whose answer must be an instance of a Pydantic model
  (follow-up questions, analysis, agent design, test verdicts, fix proposals);
- `turn(...)`: one step of an agent's tool-using conversation.
`check(...)` verifies credentials and model access without spending tokens.

Every provider says whether its output is `simulated`. Results produced by a simulated provider
are labelled as such everywhere they are stored or shown.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@dataclass
class Usage:
    """Token usage as reported by the provider for one request."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_input(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


@dataclass
class ToolUse:
    id: str
    name: str
    input: Any  # normally a dict; validated by the runtime before any tool runs


@dataclass
class Turn:
    """One model step. `content` is the provider's own representation of the assistant turn and is
    appended to the conversation unchanged, so provider-specific blocks (e.g. thinking) survive."""

    stop_reason: str  # end_turn | tool_use | max_tokens | refusal | other provider values
    text: str
    tool_uses: list[ToolUse]
    content: Any
    usage: Usage
    model: str


@dataclass
class Structured:
    value: BaseModel
    usage: Usage
    model: str


@dataclass
class ProviderStatus:
    ok: bool
    provider: str
    model: str
    message: str
    details: dict = field(default_factory=dict)


class ModelProvider(ABC):
    name: str = "provider"
    simulated: bool = False

    @abstractmethod
    def structured(self, output_type: type[T], *, system: str, prompt: str, model: str, effort: str = "high",
                   max_tokens: int = 32000) -> Structured:
        """Return an instance of `output_type`, or raise a ModelError."""

    @abstractmethod
    def turn(self, *, model: str, system: str, tools: list[dict], messages: list, effort: str = "medium",
             max_tokens: int = 16000) -> Turn:
        """Run one agent step, or raise a ModelError."""

    def check(self, model: str) -> ProviderStatus:
        return ProviderStatus(True, self.name, model, "No check available for this provider.")
