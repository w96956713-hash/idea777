"""Model constants, pricing and small helpers shared by the engine.

The engine talks to models only through a ModelProvider (see agentgen/providers). This module
holds what is provider-independent: default model, list prices, and the `structured` helper.
"""

import os

DEFAULT_MODEL = "claude-opus-5-5"

# Server-side refusal fallback (Anthropic): if a request is declined by a safety classifier, the
# API re-runs it on Anthropic's recommended fallback model in the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}

# USD per million tokens (input, output), list prices. Used for estimates and the budget; real
# invoices differ (prompt caching discounts, negotiated pricing).
PRICES = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}


def fallback_kwargs(model: str) -> dict:
    if model in _FALLBACK_MODELS:
        return {"betas": [FALLBACK_BETA], "fallbacks": "default"}
    return {}


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Estimated cost at list prices (None for unpriced models)."""
    price = PRICES.get(model)
    if price is None:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


def model_configured() -> bool:
    """True when the server has an Anthropic API key (the only credential source used)."""
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def structured(provider, output_type, system: str, prompt: str, *, model: str = DEFAULT_MODEL,
               effort: str = "high", max_tokens: int = 32000):
    """One structured-output request through a provider; returns the parsed Pydantic instance."""
    return provider.structured(output_type, system=system, prompt=prompt, model=model, effort=effort,
                               max_tokens=max_tokens).value


def make_provider():
    """The Anthropic provider configured from the server's environment (raises ModelNotConfigured
    when ANTHROPIC_API_KEY is missing)."""
    from .config import model_settings
    from .providers.anthropic_provider import AnthropicProvider

    s = model_settings()
    return AnthropicProvider(timeout=s.timeout_seconds, max_retries=s.max_retries)


# Re-exported for callers that only need the error types.
from .providers.errors import BudgetExceeded, ModelError, ModelNotConfigured, ModelRefused  # noqa: E402,F401

RefusalError = ModelRefused
