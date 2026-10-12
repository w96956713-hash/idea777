"""Shared Claude API settings and helpers."""

import os

import anthropic

DEFAULT_MODEL = "claude-opus-5-5"

# Server-side refusal fallback: if a request is declined by a safety classifier,
# the API re-runs it on Anthropic's recommended fallback model in the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}

# USD per million tokens (input, output), list prices. Used for estimates and monitoring;
# prompt caching makes real input costs lower.
PRICES = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}


class RefusalError(RuntimeError):
    """The model declined the request and no fallback could serve it."""


def make_client() -> anthropic.Anthropic:
    # Resolves credentials from ANTHROPIC_API_KEY or an `ant auth login` profile.
    return anthropic.Anthropic()


def fallback_kwargs(model: str) -> dict:
    if model in _FALLBACK_MODELS:
        return {"betas": [FALLBACK_BETA], "fallbacks": "default"}
    return {}


def check_refusal(response) -> None:
    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        raise RefusalError(f"Model declined the request (category: {category})")


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    price = PRICES.get(model)
    if price is None:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


def structured(client, output_format, system: str, prompt: str, *, model: str = DEFAULT_MODEL,
               effort: str = "high", max_tokens: int = 32000):
    """One structured-output call: returns an instance of `output_format` (a Pydantic model)."""
    with client.beta.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system,
        output_config={"effort": effort},
        output_format=output_format,
        messages=[{"role": "user", "content": prompt}],
        **fallback_kwargs(model),
    ) as stream:
        response = stream.get_final_message()
    check_refusal(response)
    parsed = getattr(response, "parsed_output", None)
    if parsed is None:
        raise RuntimeError(f"Model returned no structured result (stop_reason={response.stop_reason})")
    return parsed


# ---------------------------------------------------------------- metering & spending limits

class BudgetExceeded(RuntimeError):
    """The organisation's monthly model budget is used up; no further calls are made."""


class ModelNotConfigured(RuntimeError):
    """No model credentials are configured on the server."""


def model_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def usage_cost(model: str, usage) -> tuple[int, int, float]:
    """(input tokens incl. cache, output tokens, USD) for a response's usage block."""
    fresh = getattr(usage, "input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    price_in, price_out = PRICES.get(model, PRICES[DEFAULT_MODEL])
    usd = (fresh * price_in + cache_write * price_in * 1.25 + cache_read * price_in * 0.1 + out * price_out) / 1e6
    return fresh + cache_write + cache_read, out, usd


class _Messages:
    def __init__(self, metered):
        self._m = metered

    def create(self, **kwargs):
        self._m.check()
        response = self._m.inner.beta.messages.create(**kwargs)
        self._m.record(kwargs.get("model", DEFAULT_MODEL), response)
        return response

    def stream(self, **kwargs):
        self._m.check()
        return _MeteredStream(self._m, kwargs)


class _MeteredStream:
    def __init__(self, metered, kwargs):
        self._m, self._kwargs = metered, kwargs

    def __enter__(self):
        self._cm = self._m.inner.beta.messages.stream(**self._kwargs)
        self._stream = self._cm.__enter__()
        return self

    def get_final_message(self):
        message = self._stream.get_final_message()
        self._m.record(self._kwargs.get("model", DEFAULT_MODEL), message)
        return message

    def __exit__(self, *exc):
        return self._cm.__exit__(*exc)


class MeteredClient:
    """Wraps a client: refuses calls once the organisation's monthly budget is spent and
    records every call's tokens and cost. `spend_and_budget() -> (spent, budget)` and
    `record(purpose, model, input_tokens, output_tokens, usd)` are supplied by the caller."""

    def __init__(self, inner, purpose: str, spend_and_budget, record):
        self.inner = inner
        self.purpose = purpose
        self._spend_and_budget = spend_and_budget
        self._record = record
        self.beta = type("Beta", (), {})()
        self.beta.messages = _Messages(self)

    def check(self):
        spent, budget = self._spend_and_budget()
        if spent >= budget:
            raise BudgetExceeded(f"Monthly model budget of ${budget:.2f} is used up (${spent:.2f} spent). "
                                 "Raise it in Settings to continue.")

    def record(self, model: str, response) -> None:
        usage = getattr(response, "usage", None)
        if usage is not None:
            tokens_in, tokens_out, usd = usage_cost(model, usage)
            self._record(self.purpose, model, tokens_in, tokens_out, usd)
