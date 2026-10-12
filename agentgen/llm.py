"""Shared Claude API settings and helpers."""

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
