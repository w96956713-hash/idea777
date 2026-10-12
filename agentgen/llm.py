"""Shared Claude API settings."""

import anthropic

DEFAULT_MODEL = "claude-opus-5-5"

# Server-side refusal fallback: if a request is declined by a safety classifier,
# the API re-runs it on Anthropic's recommended fallback model in the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}


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
