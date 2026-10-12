"""Server configuration, read from environment variables. Nothing secret is ever returned by
`public()`: only whether the API key is present."""

import os
from dataclasses import dataclass

from .llm import DEFAULT_MODEL, PRICES


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class ModelSettings:
    default_model: str      # new agents and the requirements pipeline
    grader_model: str       # judges test results
    timeout_seconds: float  # per request
    max_retries: int        # SDK retries for rate limits, overloads, connection errors

    @property
    def api_key_present(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())

    def public(self) -> dict:
        return {
            "provider": "anthropic",
            "api_key_configured": self.api_key_present,
            "default_model": self.default_model,
            "grader_model": self.grader_model,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "known_models": list(PRICES),
            "unpriced_models": [m for m in {self.default_model, self.grader_model} if m not in PRICES],
        }


def model_settings() -> ModelSettings:
    default = os.environ.get("AGENTGEN_DEFAULT_MODEL", "").strip() or DEFAULT_MODEL
    return ModelSettings(
        default_model=default,
        grader_model=os.environ.get("AGENTGEN_GRADER_MODEL", "").strip() or default,
        timeout_seconds=_float("AGENTGEN_MODEL_TIMEOUT_SECONDS", 600.0),
        max_retries=max(0, min(_int("AGENTGEN_MODEL_MAX_RETRIES", 2), 5)),
    )


def default_budget_usd() -> float:
    return max(0.0, _float("AGENTGEN_DEFAULT_BUDGET_USD", 25.0))
