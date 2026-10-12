"""Anthropic Claude provider, built on the official Anthropic Python SDK.

- The API key comes only from the server-side ANTHROPIC_API_KEY environment variable (or an
  explicit argument in tests). It is passed explicitly, so the SDK never falls back to other
  credential sources, and requests always go to the official API endpoint.
- Requests are streamed (no HTTP timeouts on long answers) and use structured outputs for
  anything the engine parses.
- SDK exceptions are translated into provider-neutral ModelErrors with user-facing messages.
  The SDK retries rate limits, overloads and connection errors itself (`max_retries`).
"""

import json
import os
from contextlib import contextmanager

import anthropic
from pydantic import ValidationError

from ..llm import fallback_kwargs
from .base import ModelProvider, ProviderStatus, Structured, ToolUse, Turn, Usage
from .errors import (ModelAuthError, ModelBillingError, ModelError, ModelNotConfigured, ModelOutputInvalid,
                     ModelRateLimited, ModelRefused, ModelRequestError, ModelTimeout, ModelUnavailable)

API_URL = "https://api.anthropic.com"


def _usage(u) -> Usage:
    if u is None:
        return Usage()
    return Usage(
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
    )


def _api_message(e: anthropic.APIStatusError) -> str:
    """The API's own error text (never contains the key)."""
    try:
        body = e.response.json()
        return str(body.get("error", {}).get("message") or e.message)[:500]
    except Exception:
        return str(getattr(e, "message", "") or "")[:500]


@contextmanager
def translate_errors():
    """Map SDK exceptions to ModelErrors. Order matters: specific classes first."""
    try:
        yield
    except ModelError:
        raise
    except anthropic.APITimeoutError:
        raise ModelTimeout("The model did not answer in time.")
    except anthropic.APIConnectionError:
        raise ModelUnavailable("Could not reach the model service.")
    except anthropic.AuthenticationError:
        raise ModelAuthError("The model service rejected the server's API key.")
    except anthropic.PermissionDeniedError as e:
        raise ModelAuthError(f"The API key is not allowed to do this: {_api_message(e)}")
    except anthropic.RateLimitError as e:
        retry_after = None
        try:
            retry_after = float(e.response.headers.get("retry-after"))
        except (TypeError, ValueError):
            pass
        raise ModelRateLimited("The model service is rate limiting requests.", retry_after=retry_after)
    except anthropic.NotFoundError as e:
        raise ModelRequestError(f"Model or endpoint not found: {_api_message(e)}")
    except anthropic.BadRequestError as e:
        raise ModelRequestError(f"The model service rejected the request: {_api_message(e)}")
    except anthropic.APIStatusError as e:
        status = getattr(e, "status_code", 0)
        if status == 402:
            raise ModelBillingError(f"Billing problem with the API account: {_api_message(e)}")
        if status == 413:
            raise ModelRequestError("The request is too large for the model service.",
                                    hint="Shorten the requirements, documents or task.")
        if status in (408, 504):
            raise ModelTimeout("The model service timed out.")
        if status >= 500 or status == 429:
            raise ModelUnavailable(f"The model service had a problem ({status}).")
        raise ModelRequestError(f"The model service returned an error ({status}): {_api_message(e)}")
    except (ValidationError, json.JSONDecodeError, ValueError) as e:
        # Raised while parsing a streamed or structured answer that is malformed.
        raise ModelOutputInvalid(f"The model's answer could not be read ({type(e).__name__}).")


class AnthropicProvider(ModelProvider):
    name = "anthropic"
    simulated = False

    def __init__(self, *, api_key: str | None = None, timeout: float = 600.0, max_retries: int = 2,
                 http_client=None):
        key = (api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")).strip()
        if not key:
            raise ModelNotConfigured("No model API key is configured on the server.")
        kwargs = {"api_key": key, "base_url": API_URL, "timeout": timeout, "max_retries": max_retries}
        if http_client is not None:
            kwargs["http_client"] = http_client
        self._client = anthropic.Anthropic(**kwargs)

    # ------------------------------------------------------------------ structured answers

    def structured(self, output_type, *, system, prompt, model, effort="high", max_tokens=32000) -> Structured:
        # The schema is sent as a strict JSON-schema output format (the SDK's own transform), and the
        # answer is parsed only after the whole message has arrived, so a cut-off answer is reported
        # as cut off rather than as malformed.
        schema = anthropic.transform_schema(output_type)
        with translate_errors():
            with self._client.beta.messages.stream(
                model=model,
                max_tokens=max_tokens,
                system=system,
                output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
                messages=[{"role": "user", "content": prompt}],
                **fallback_kwargs(model),
            ) as stream:
                message = stream.get_final_message()
        usage = _usage(getattr(message, "usage", None))
        if message.stop_reason == "refusal":
            raise ModelRefused("The model declined to answer this request.")
        if message.stop_reason == "max_tokens":
            raise ModelOutputInvalid("The model's answer was cut off before it was complete.")
        text = "".join(getattr(b, "text", "") for b in message.content if getattr(b, "type", "") == "text")
        try:
            value = output_type.model_validate_json(text)
        except (ValidationError, ValueError):
            raise ModelOutputInvalid("The model's answer did not match the expected structure.")
        return Structured(value=value, usage=usage, model=getattr(message, "model", model) or model)

    # ------------------------------------------------------------------ agent turns

    def turn(self, *, model, system, tools, messages, effort="medium", max_tokens=16000) -> Turn:
        # Streamed with eager tool-input streaming; the runtime validates every tool input
        # against its schema before running anything, and never runs tools from a turn that
        # stopped at max_tokens or was refused.
        stream_tools = [{**t, "eager_input_streaming": True} for t in tools]
        with translate_errors():
            with self._client.beta.messages.stream(
                model=model,
                max_tokens=max_tokens,
                system=system,
                tools=stream_tools,
                messages=messages,
                output_config={"effort": effort},
                cache_control={"type": "ephemeral"},
                **fallback_kwargs(model),
            ) as stream:
                message = stream.get_final_message()
        tool_uses, texts = [], []
        for block in message.content:
            kind = getattr(block, "type", "")
            if kind == "tool_use":
                tool_uses.append(ToolUse(id=block.id, name=block.name, input=block.input))
            elif kind == "text":
                texts.append(block.text)
        return Turn(stop_reason=message.stop_reason or "end_turn", text="\n".join(texts).strip(),
                    tool_uses=tool_uses, content=message.content, usage=_usage(getattr(message, "usage", None)),
                    model=getattr(message, "model", model) or model)

    # ------------------------------------------------------------------ credentials check

    def check(self, model: str) -> ProviderStatus:
        """Verify the key and that the model is available, via the Models API (no tokens used)."""
        try:
            with translate_errors():
                info = self._client.models.retrieve(model)
        except ModelError as e:
            return ProviderStatus(False, self.name, model, e.user_message, {"error": type(e).__name__})
        return ProviderStatus(True, self.name, model, f"Connected. Model {getattr(info, 'id', model)} is available.",
                              {"display_name": getattr(info, "display_name", None)})
