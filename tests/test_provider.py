"""Tests of the Anthropic provider against mocked HTTP (the official SDK's own transport hook).
No request ever leaves the process: every response is produced by httpx2.MockTransport."""

import json

import httpx2
import pytest
from anthropic import DefaultHttpxClient
from pydantic import BaseModel

from agentgen.llm import make_provider, model_configured
from agentgen.providers import (BudgetExceeded, ModelAuthError, ModelBillingError, ModelNotConfigured,
                                ModelOutputInvalid, ModelRateLimited, ModelRefused, ModelRequestError, ModelTimeout,
                                ModelUnavailable, Usage)
from agentgen.providers.anthropic_provider import AnthropicProvider
from agentgen.providers.metering import MeteredProvider, estimate_cost_usd

KEY = "sk-ant-test-0000-not-a-real-key"
MODEL = "claude-opus-5-5"


class Answer(BaseModel):
    title: str
    count: int


def sse(events) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def message_events(blocks, stop_reason="end_turn", usage=None):
    """SSE events for a streamed message. blocks: ("text", str) | ("tool_use", id, name, dict)."""
    usage = usage or {"input_tokens": 120, "output_tokens": 40, "cache_read_input_tokens": 1000,
                      "cache_creation_input_tokens": 50}
    events = [{"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": MODEL, "content": [], "stop_reason": None,
        "stop_sequence": None, "usage": {"input_tokens": usage["input_tokens"], "output_tokens": 1,
                                         "cache_read_input_tokens": usage["cache_read_input_tokens"],
                                         "cache_creation_input_tokens": usage["cache_creation_input_tokens"]}}}]
    for i, block in enumerate(blocks):
        if block[0] == "text":
            events += [{"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}},
                       {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": block[1]}},
                       {"type": "content_block_stop", "index": i}]
        else:
            _, tid, name, args = block
            events += [{"type": "content_block_start", "index": i,
                        "content_block": {"type": "tool_use", "id": tid, "name": name, "input": {}}},
                       {"type": "content_block_delta", "index": i,
                        "delta": {"type": "input_json_delta", "partial_json": json.dumps(args)}},
                       {"type": "content_block_stop", "index": i}]
    events += [{"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": usage["output_tokens"]}},
               {"type": "message_stop"}]
    return sse(events)


class Recorder:
    """A MockTransport handler that records requests and replies from a queue."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, request: httpx2.Request):
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    @property
    def body(self):
        return json.loads(self.requests[-1].content)


def stream_response(body: bytes) -> httpx2.Response:
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)


def error_response(status: int, message: str = "nope", headers=None, kind="api_error") -> httpx2.Response:
    return httpx2.Response(status, headers=headers or {},
                           json={"type": "error", "error": {"type": kind, "message": message}})


def provider(handler, **kw) -> AnthropicProvider:
    return AnthropicProvider(api_key=KEY, max_retries=kw.pop("max_retries", 0),
                             http_client=DefaultHttpxClient(transport=httpx2.MockTransport(handler)), **kw)


# ---------------------------------------------------------------- credentials


def test_missing_key_never_calls_the_api(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "some-other-credential")  # must be ignored
    assert model_configured() is False
    with pytest.raises(ModelNotConfigured) as e:
        make_provider()
    assert "ANTHROPIC_API_KEY" in e.value.user_message
    with pytest.raises(ModelNotConfigured):
        AnthropicProvider(api_key="   ")


def test_key_only_from_env_and_requests_go_to_the_official_endpoint(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://attacker.example")  # must not redirect the key
    rec = Recorder(stream_response(message_events([("text", '{"title": "ok", "count": 2}')])))
    p = AnthropicProvider(http_client=DefaultHttpxClient(transport=httpx2.MockTransport(rec)), max_retries=0)
    p.structured(Answer, system="s", prompt="p", model=MODEL)
    req = rec.requests[0]
    assert req.url.host == "api.anthropic.com"
    assert req.headers["x-api-key"] == KEY and "authorization" not in req.headers


# ---------------------------------------------------------------- structured outputs


def test_structured_output_request_and_parsing():
    rec = Recorder(stream_response(message_events([("text", '{"title": "Weekly report", "count": 4}')])))
    result = provider(rec).structured(Answer, system="sys", prompt="Write it", model=MODEL, effort="high",
                                      max_tokens=8000)
    assert result.value == Answer(title="Weekly report", count=4)
    assert result.usage == Usage(input_tokens=120, output_tokens=40, cache_read_tokens=1000, cache_write_tokens=50)
    body = rec.body
    assert body["model"] == MODEL and body["max_tokens"] == 8000 and body["stream"] is True
    assert body["output_config"]["effort"] == "high"
    schema = body["output_config"]["format"]["schema"]
    assert schema["required"] == ["title", "count"] and schema["additionalProperties"] is False
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in rec.requests[0].headers["anthropic-beta"]


def test_malformed_and_cut_off_answers():
    bad_json = Recorder(stream_response(message_events([("text", '{"title": "missing count"}')])))
    with pytest.raises(ModelOutputInvalid):
        provider(bad_json).structured(Answer, system="s", prompt="p", model=MODEL)
    cut = Recorder(stream_response(message_events([("text", '{"title": "Wee')], stop_reason="max_tokens")))
    with pytest.raises(ModelOutputInvalid, match="cut off"):
        provider(cut).structured(Answer, system="s", prompt="p", model=MODEL)
    refused = Recorder(stream_response(message_events([], stop_reason="refusal")))
    with pytest.raises(ModelRefused):
        provider(refused).structured(Answer, system="s", prompt="p", model=MODEL)


# ---------------------------------------------------------------- agent turns


def test_agent_turn_with_tool_use():
    rec = Recorder(stream_response(message_events(
        [("text", "Let me check."), ("tool_use", "toolu_1", "sales_data", {"sql": "SELECT 1"})], stop_reason="tool_use")))
    tools = [{"name": "sales_data", "description": "Run SQL", "strict": True,
              "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"],
                               "additionalProperties": False}}]
    turn = provider(rec).turn(model=MODEL, system="sys", tools=tools,
                              messages=[{"role": "user", "content": "revenue?"}], effort="medium")
    assert turn.stop_reason == "tool_use" and turn.text == "Let me check."
    assert [(u.id, u.name, u.input) for u in turn.tool_uses] == [("toolu_1", "sales_data", {"sql": "SELECT 1"})]
    assert turn.usage.total_input == 1170
    body = rec.body
    assert body["tools"][0]["eager_input_streaming"] is True and body["tools"][0]["name"] == "sales_data"
    assert body["cache_control"] == {"type": "ephemeral"} and body["output_config"]["effort"] == "medium"
    assert "eager_input_streaming" not in tools[0]  # caller's definitions are not mutated


# ---------------------------------------------------------------- errors


@pytest.mark.parametrize("status,kind,error_type,headers", [
    (401, "authentication_error", ModelAuthError, None),
    (403, "permission_error", ModelAuthError, None),
    (402, "billing_error", ModelBillingError, None),
    (429, "rate_limit_error", ModelRateLimited, {"retry-after": "30"}),
    (500, "api_error", ModelUnavailable, None),
    (529, "overloaded_error", ModelUnavailable, None),
    (400, "invalid_request_error", ModelRequestError, None),
    (404, "not_found_error", ModelRequestError, None),
    (413, "request_too_large", ModelRequestError, None),
])
def test_api_errors_are_translated(status, kind, error_type, headers):
    rec = Recorder(error_response(status, f"details for {status}", headers, kind))
    with pytest.raises(error_type) as e:
        provider(rec).structured(Answer, system="s", prompt="p", model=MODEL)
    assert KEY not in str(e.value) and KEY not in e.value.user_message
    assert e.value.hint  # every error tells the user what to do
    if status == 429:
        assert e.value.retry_after == 30 and e.value.retryable
    if status == 400:
        assert "details for 400" in str(e.value)  # the API's own explanation is kept


def test_timeouts_and_connection_failures():
    timeout = Recorder(httpx2.ReadTimeout("slow"))
    with pytest.raises(ModelTimeout):
        provider(timeout).structured(Answer, system="s", prompt="p", model=MODEL)
    down = Recorder(httpx2.ConnectError("no route"))
    with pytest.raises(ModelUnavailable):
        provider(down).turn(model=MODEL, system="s", tools=[], messages=[{"role": "user", "content": "x"}])


def test_sdk_retries_transient_errors():
    rec = Recorder(error_response(529, "busy", {"retry-after-ms": "1"}, "overloaded_error"),
                   stream_response(message_events([("text", '{"title": "ok", "count": 1}')])))
    result = provider(rec, max_retries=2).structured(Answer, system="s", prompt="p", model=MODEL)
    assert result.value.count == 1 and len(rec.requests) == 2


def test_connection_check_uses_no_tokens():
    ok = Recorder(httpx2.Response(200, json={"id": MODEL, "type": "model", "display_name": "Claude Opus 5.5",
                                             "created_at": "2026-01-01T00:00:00Z"}))
    status = provider(ok).check(MODEL)
    assert status.ok and MODEL in status.message
    assert ok.requests[0].url.path.endswith(f"/v1/models/{MODEL}") and ok.requests[0].method == "GET"
    bad = Recorder(error_response(401, "invalid x-api-key", None, "authentication_error"))
    status = provider(bad).check(MODEL)
    assert not status.ok and "ANTHROPIC_API_KEY" in status.message


# ---------------------------------------------------------------- metering


def test_budget_is_enforced_before_any_request():
    rec = Recorder()  # no replies: any request would fail the test
    recorded = []
    metered = MeteredProvider(provider(rec), "test", lambda simulated: (24.99, 25.0),
                              lambda *a: recorded.append(a))
    with pytest.raises(BudgetExceeded):
        metered.structured(Answer, system="s", prompt="p", model=MODEL)
    assert rec.requests == [] and recorded == []


def test_provider_reported_usage_is_recorded():
    rec = Recorder(stream_response(message_events([("text", '{"title": "ok", "count": 1}')])))
    recorded = []
    metered = MeteredProvider(provider(rec), "requirements", lambda simulated: (0.0, 25.0),
                              lambda *a: recorded.append(a))
    metered.structured(Answer, system="s", prompt="p", model=MODEL)
    purpose, name, model, usage, usd, simulated = recorded[0]
    assert (purpose, name, model, simulated) == ("requirements", "anthropic", MODEL, False)
    assert usage == Usage(120, 40, 1000, 50)
    assert usd == pytest.approx(estimate_cost_usd(MODEL, usage))
    assert usd == pytest.approx((120 * 4 + 50 * 4 * 1.25 + 1000 * 4 * 0.1 + 40 * 20) / 1e6)
