"""Operational metrics for an agent: volume, outcomes, errors, approvals and cost."""

import json
from collections import Counter
from datetime import timedelta

from .llm import cost_usd
from .store import Store, utcnow


def agent_metrics(store: Store, agent_name: str, days: int = 30) -> dict:
    agent = store.agent(agent_name)
    since = (utcnow() - timedelta(days=days)).isoformat()
    aid = agent["id"]
    runs = store.conn.execute("SELECT * FROM runs WHERE agent_id = ? AND created_at >= ?", (aid, since)).fetchall()

    models: dict[int, str] = {}
    by_version: dict[int, dict] = {}
    total_cost, unknown_cost = 0.0, False
    for r in runs:
        if r["version"] not in models:
            models[r["version"]] = store.load_spec(agent_name, r["version"])[0].model
        cost = cost_usd(models[r["version"]], r["input_tokens"], r["output_tokens"])
        if cost is None:
            unknown_cost = True
        else:
            total_cost += cost
        v = by_version.setdefault(r["version"], {"runs": 0, "completed": 0})
        v["runs"] += 1
        v["completed"] += r["stop_reason"] == "end_turn"

    events = Counter(row["event"] for row in store.conn.execute(
        "SELECT event FROM audit_log WHERE agent_id = ? AND created_at >= ?", (aid, since)))
    decisions = Counter(json.loads(row["data_json"]).get("decision") for row in store.conn.execute(
        "SELECT data_json FROM audit_log WHERE agent_id = ? AND event = 'approval' AND created_at >= ?", (aid, since)))
    escalations = store.conn.execute(
        "SELECT COUNT(*) FROM escalations WHERE agent_id = ? AND created_at >= ?", (aid, since)).fetchone()[0]
    failed_jobs = store.conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE agent_id = ? AND status = 'failed' AND created_at >= ?", (aid, since)).fetchone()[0]

    n = len(runs)
    tool_calls = events["tool_call"] + events["tool_error"]
    return {
        "agent": agent_name,
        "deployed_version": agent["deployed_version"],
        "days": days,
        "runs": n,
        "stop_reasons": dict(Counter(r["stop_reason"] for r in runs)),
        "completion_rate": (sum(r["stop_reason"] == "end_turn" for r in runs) / n) if n else None,
        "escalations": escalations,
        "escalation_rate": escalations / n if n else None,
        "tool_calls": tool_calls,
        "tool_error_rate": events["tool_error"] / tool_calls if tool_calls else None,
        "blocked_tool_calls": events["tool_blocked"],
        "approvals": dict(decisions),
        "pending_actions": len(store.pending_actions(aid)),
        "failed_jobs": failed_jobs,
        "input_tokens": sum(r["input_tokens"] for r in runs),
        "output_tokens": sum(r["output_tokens"] for r in runs),
        "cost_usd": round(total_cost, 4),
        "cost_usd_per_run": round(total_cost / n, 4) if n else None,
        "avg_duration_ms": (sum(r["duration_ms"] for r in runs if r["duration_ms"] is not None)
                            / max(1, sum(1 for r in runs if r["duration_ms"] is not None)))
                           if any(r["duration_ms"] is not None for r in runs) else None,
        "by_source": dict(Counter(r["trigger_source"] for r in runs)),
        "failed_runs": sum(1 for r in runs if r["stop_reason"] not in ("end_turn", None)),
        "cost_note": "list prices, before prompt-caching discounts" + ("; some models unpriced" if unknown_cost else ""),
        "by_version": {k: {**v, "completion_rate": v["completed"] / v["runs"]} for k, v in sorted(by_version.items())},
    }
