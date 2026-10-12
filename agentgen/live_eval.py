"""First real-model evaluation: a small, capped check of the platform against the real model.

    agentgen live-eval --budget 5

- Refuses to start without ANTHROPIC_API_KEY (nothing is called).
- Checks the key and model through the Models API first (no tokens).
- Works in its own throwaway database, never the application's data.
- Runs the bundled weekly-sales example's test suite, whose correct figures are known, so the
  results are checked by exact assertions (figures, tools used, forbidden tools) as well as by
  the grader model.
- Every model call is metered against a hard budget cap; when the next call would exceed it,
  the evaluation stops and says so.

This is the only part of the project meant to call the real API, and only when a person runs it.
"""

import json
import tempfile
from pathlib import Path

from . import datasets
from .config import model_settings
from .designer import validate_design
from .evaluate import evaluate
from .llm import make_provider, model_configured
from .providers import ModelNotConfigured
from .providers.metering import metered
from .spec import AgentSpec
from .store import Store

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "sales_insights"
CUSTOMER = "Live evaluation"

# Exact facts from examples/sales_insights/data that a correct answer must state.
EXACT_FIGURES = {
    "revenue by region": ["10325", "3294", "2297", "4735"],
}


def _example_spec(model: str) -> AgentSpec:
    spec = validate_design(AgentSpec.from_yaml((EXAMPLE / "spec.yaml").read_text(encoding="utf-8")))
    spec.model = model
    for case in spec.test_cases:
        case.must_include = list(EXACT_FIGURES.get(case.name, case.must_include))
    return spec


def run_live_eval(budget_usd: float = 5.0, workdir: str | Path | None = None, model: str | None = None,
                  provider=None, out=print) -> dict:
    """Run the evaluation and return a report (also written to <workdir>/live_eval_report.json).
    `provider` is for tests only; normally the Anthropic provider is built from the environment."""
    if budget_usd <= 0:
        raise ValueError("The budget must be more than 0 USD")
    if not (EXAMPLE / "spec.yaml").exists():
        raise ValueError(f"The example agent was not found at {EXAMPLE}; run this from a source checkout")
    if provider is None:
        if not model_configured():
            raise ModelNotConfigured("No model API key is configured on the server.")
        provider = make_provider()
    settings = model_settings()
    model = model or settings.default_model

    out(f"1/3 Checking the API key and model {model} (Models API, no tokens)...")
    status = provider.check(model)
    out(f"    {status.message}")
    if not status.ok:
        return {"ok": False, "stage": "connection", "message": status.message}

    folder = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="agentgen-live-eval-"))
    folder.mkdir(parents=True, exist_ok=True)
    db = folder / "live_eval.db"
    store = Store(str(db))
    cid = store.get_or_create_customer(CUSTOMER)
    store.set_monthly_budget(cid, budget_usd)
    spent_before = store.month_spend(cid, simulated=bool(provider.simulated))
    if spent_before >= budget_usd:
        raise ValueError(f"This evaluation database already spent {spent_before:.2f} USD; use a new --dir or a "
                         "higher --budget")
    for name in ("orders", "products"):
        datasets.import_csv(store, cid, name, EXAMPLE / "data" / f"{name}.csv")
    spec = _example_spec(model)
    version = store.save_version(cid, spec, origin="imported", note="Live evaluation of the bundled sales example")

    out(f"2/3 Running {len(spec.test_cases)} test cases of {spec.name} with {model} "
        f"(budget {budget_usd:.2f} USD, database {db})...")
    capped = metered(provider, lambda: Store(str(db), migrate=False), cid, "live-eval", budget_usd)

    def progress(case):
        mark = "pass" if case.passed else "FAIL"
        cost = f"{case.usd:.4f} USD" if case.usd is not None else "-"
        out(f"    [{mark}] {case.name} ({case.category}) · {case.input_tokens} in / {case.output_tokens} out "
            f"tokens · {cost}")
        if not case.passed:
            out(f"           checks: {case.tool_check} | grader: {case.judge_reasoning[:200]}"
                + (f" | error: {case.error}" if case.error else ""))

    try:
        report = evaluate(capped, store, spec.name, version, on_case=progress)
        spent = store.month_spend(cid, simulated=bool(provider.simulated)) - spent_before
    finally:
        store.close()
    result = {
        "ok": True, "stage": "done", "passed": report.passed, "pass_rate": report.pass_rate,
        "blockers": report.blockers(), "model": model, "provider": provider.name,
        "simulated": bool(provider.simulated), "estimated_cost_usd": round(spent, 4), "budget_usd": budget_usd,
        "database": str(db), "report": report.to_dict(),
    }
    (folder / "live_eval_report.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    out(f"3/3 {'PASSED' if report.passed else 'DID NOT PASS'}: {report.pass_rate:.0%} of cases; "
        f"estimated cost {spent:.4f} USD (list prices, provider-reported tokens).")
    for blocker in report.blockers():
        out(f"    - {blocker}")
    if provider.simulated:
        out("    NOTE: a simulated provider was used; this says nothing about real model quality.")
    out(f"    Full report: {folder / 'live_eval_report.json'}")
    return result
