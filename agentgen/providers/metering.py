"""Budget enforcement and usage recording around any provider.

Before every call the organisation's month-to-date spend plus an estimate of this call is
compared with its budget; the call is refused (no request is sent) if it would exceed it.
After the call, the token usage reported by the provider is recorded with an estimated cost
(list prices). Simulated providers are recorded and labelled too, so test-mode activity never
looks like real spend.
"""

import json

from ..llm import DEFAULT_MODEL, PRICES
from .base import ModelProvider, ProviderStatus, Structured, Turn, Usage
from .errors import BudgetExceeded

# Expected output for the pre-call estimate; the real figure is recorded afterwards.
EXPECTED_OUTPUT_TOKENS = 4000
CHARS_PER_TOKEN = 3.5


def estimate_cost_usd(model: str, usage: Usage) -> float:
    """Estimated cost at list prices: cache writes at 1.25x, cache reads at 0.1x the input price."""
    price_in, price_out = PRICES.get(model, PRICES[DEFAULT_MODEL])
    return (usage.input_tokens * price_in + usage.cache_write_tokens * price_in * 1.25
            + usage.cache_read_tokens * price_in * 0.1 + usage.output_tokens * price_out) / 1_000_000


def projected_cost_usd(model: str, payload_chars: int, max_tokens: int) -> float:
    price_in, price_out = PRICES.get(model, PRICES[DEFAULT_MODEL])
    est_in = payload_chars / CHARS_PER_TOKEN
    est_out = min(max_tokens, EXPECTED_OUTPUT_TOKENS)
    return (est_in * price_in + est_out * price_out) / 1_000_000


class MeteredProvider(ModelProvider):
    """Wraps a provider. `spend_and_budget(simulated) -> (spent_usd, budget_usd)` (spend counted
    separately for real and simulated usage, so test-mode activity never uses up a real budget) and
    `record(purpose, provider, model, usage, usd, simulated)` are supplied by the caller."""

    def __init__(self, inner: ModelProvider, purpose: str, spend_and_budget, record):
        self.inner = inner
        self.purpose = purpose
        self.name = inner.name
        self.simulated = inner.simulated
        self._spend_and_budget = spend_and_budget
        self._record = record

    def _guard(self, model: str, payload_chars: int, max_tokens: int) -> None:
        spent, budget = self._spend_and_budget(self.inner.simulated)
        projected = projected_cost_usd(model, payload_chars, max_tokens)
        if spent >= budget or spent + projected > budget:
            raise BudgetExceeded(
                f"The monthly model budget of ${budget:.2f} would be exceeded "
                f"(${spent:.2f} spent, this request is estimated at ${projected:.2f}).")

    def _after(self, model: str, usage: Usage) -> None:
        self._record(self.purpose, self.inner.name, model, usage, estimate_cost_usd(model, usage), self.inner.simulated)

    def structured(self, output_type, *, system, prompt, model, effort="high", max_tokens=32000) -> Structured:
        self._guard(model, len(system) + len(prompt), max_tokens)
        result = self.inner.structured(output_type, system=system, prompt=prompt, model=model, effort=effort,
                                       max_tokens=max_tokens)
        self._after(model, result.usage)
        return result

    def turn(self, *, model, system, tools, messages, effort="medium", max_tokens=16000) -> Turn:
        size = len(system) + len(json.dumps(tools, default=str)) + len(json.dumps(messages, default=str))
        self._guard(model, size, max_tokens)
        result = self.inner.turn(model=model, system=system, tools=tools, messages=messages, effort=effort,
                                 max_tokens=max_tokens)
        self._after(model, result.usage)
        return result

    def check(self, model: str) -> ProviderStatus:
        return self.inner.check(model)  # no tokens, no budget needed


def store_metering(open_store, customer_id: int, default_budget_usd: float):
    """Budget and usage callbacks backed by the platform database. `open_store()` returns a Store;
    each callback opens and closes its own connection, so it is safe from any thread."""

    def spend_and_budget(simulated: bool = False):
        st = open_store()
        try:
            return (st.month_spend(customer_id, simulated=simulated),
                    st.monthly_budget(customer_id, default_budget_usd))
        finally:
            st.close()

    def record(purpose, provider, model, usage: Usage, usd, simulated):
        st = open_store()
        try:
            st.record_usage(customer_id, purpose, model, usage.total_input, usage.output_tokens, usd, provider,
                            simulated)
        finally:
            st.close()

    return spend_and_budget, record


def metered(inner: ModelProvider, open_store, customer_id: int, purpose: str,
            default_budget_usd: float) -> MeteredProvider:
    spend_and_budget, record = store_metering(open_store, customer_id, default_budget_usd)
    return MeteredProvider(inner, purpose, spend_and_budget, record)
