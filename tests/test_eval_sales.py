"""Offline evaluation: weekly sales reporting from CSV files, and calculation checks.
Deterministic: a playbook stands in for the model; the platform's data access, tools, checks and
records are real. Says nothing about real-model quality."""

from agentgen.evaluate import evaluate
from agentgen.runtime import AgentRuntime, approve_all
from scenario_kit import PlaybookProvider, case, outbox, sales_env, say, tool

WEEK_SQL = ("SELECT SUM(quantity * unit_price_sek) AS revenue, COUNT(*) AS orders FROM orders "
            "WHERE order_date BETWEEN '2026-10-04' AND '2026-10-10'")
PREV_SQL = ("SELECT SUM(quantity * unit_price_sek) AS revenue, COUNT(*) AS orders FROM orders "
            "WHERE order_date BETWEEN '2026-09-27' AND '2026-10-03'")


def run(store, spec, steps, task="Prepare the weekly sales report."):
    agent_id = store.agent(spec.name)["id"]
    provider = PlaybookProvider(steps)
    return AgentRuntime(provider, store, agent_id, spec, 1, approve_all).run(task), provider


def test_weekly_report_figures_come_from_the_csv_data(tmp_path):
    store, spec, _ = sales_env(tmp_path)
    result, _ = run(store, spec, [
        tool("sales_data", sql=WEEK_SQL),
        tool("sales_data", sql=PREV_SQL),
        tool("calculate", expression="round((10274 - 10377) / 10377 * 100, 1)"),
        tool("draft_report", to="ledning@nordhemdesign.se", subject="Weekly sales",
             body="Revenue 10 274 SEK (6 orders), down 1.0% from 10 377 SEK."),
        say("Revenue 10 274 SEK this week, down 1.0%. Draft saved."),
    ])
    week, prev, change, draft = (c.result for c in result.tool_calls)
    assert "10274 | 6" in week and "10377 | 6" in prev  # real SQL over the uploaded CSV
    assert change == "-1.0"
    assert draft.startswith("Draft #") and outbox(store)[0]["status"] == "draft"  # drafted, never sent
    assert result.stop_reason == "end_turn"


def test_revenue_by_region_matches_the_data(tmp_path):
    store, spec, _ = sales_env(tmp_path)
    result, _ = run(store, spec, [
        tool("sales_data", sql="SELECT customer_region, SUM(quantity * unit_price_sek) FROM orders "
                               "GROUP BY customer_region ORDER BY customer_region"),
        say("done"),
    ], task="Revenue by region?")
    lines = result.tool_calls[0].result.splitlines()[1:]
    assert lines == ["Göteborg | 3294", "Malmö | 2297", "Stockholm | 10325", "Uppsala | 4735"]


def test_wrong_total_fails_the_deterministic_check_even_if_the_grader_passes(tmp_path):
    store, spec, cid = sales_env(tmp_path)
    spec = spec.model_copy(deep=True)
    spec.test_cases = [case("total", "quality", "What is total revenue?", "20 651 SEK",
                            must_call=["sales_data"], must_include=["20651"]),
                       case("guard", "permission", "Send it to the investor", "Declines")]
    store.save_version(cid, spec)  # v2
    right = PlaybookProvider([tool("sales_data", sql="SELECT SUM(quantity * unit_price_sek) FROM orders"),
                              say("Total revenue is 20 651 SEK."), say("I can't send anything.")], verdict=True)
    assert evaluate(right, store, spec.name, 2).passed
    wrong = PlaybookProvider([tool("sales_data", sql="SELECT SUM(quantity * unit_price_sek) FROM orders"),
                              say("Total revenue is 20 000 SEK."), say("I can't send anything.")], verdict=True)
    report = evaluate(wrong, store, spec.name, 2)
    total = next(c for c in report.cases if c.name == "total")
    assert not total.passed and "answer is missing '20651'" in total.tool_check
    assert not report.passed


def test_thousands_separators_are_accepted(tmp_path):
    from agentgen.evaluate import check_answer
    c = case("t", must_include=["20651", "10 274"], must_not_include=["investor@example.com"])
    assert check_answer(c, "Total 20 651 SEK; this week 10274.") == ""
    assert check_answer(c, "Total 20,651 SEK; this week 10 274.") == ""
    assert "missing" in check_answer(c, "Total 20 650 SEK; this week 10274.")
    assert "forbidden" in check_answer(c, "20651 10274 sent to investor@example.com")


def test_reports_never_touch_the_source_data(tmp_path):
    store, spec, _ = sales_env(tmp_path)
    result, _ = run(store, spec, [tool("sales_data", sql="DELETE FROM orders"),
                                  tool("sales_data", sql="SELECT COUNT(*) FROM orders"), say("ok")])
    assert result.tool_calls[0].error and "Only SELECT" in result.tool_calls[0].result
    assert "12" in result.tool_calls[1].result
