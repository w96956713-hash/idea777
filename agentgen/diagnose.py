"""Diagnose failed runs and test cases, and turn fixes into new, tested versions.

- `diagnose_run` and `diagnose_case` are rule-based: they read what was recorded (stop reason,
  errors, tool calls, approvals, test checks) and explain it with a concrete suggestion. They need
  no model and are always available.
- `propose_fix` asks a model for a revised specification plus a regression test case that
  captures the failure. Nothing changes until a person applies it; `apply_fix` produces a new
  draft version that must pass its tests (including the new regression case) before approval.
"""

import json
from typing import Literal

from pydantic import BaseModel, Field

from .config import model_settings
from .designer import validate_design
from .llm import structured
from .spec import AgentSpec, TestCase
from .store import Store


class Finding(BaseModel):
    severity: Literal["error", "warning", "info"]
    title: str
    detail: str
    suggestion: str


def _error_suggestion(error: str) -> str:
    e = error.lower()
    if "budget" in e:
        return "Raise the monthly budget in Settings, or wait for next month."
    if "api key" in e or "anthropic_api_key" in e:
        return "Check ANTHROPIC_API_KEY on the server (Settings → Check connection)."
    if "rate limit" in e or "overloaded" in e or "unavailable" in e or "did not answer in time" in e:
        return "A temporary problem with the model service. Run the task again."
    if "cut off" in e or "could not be read" in e:
        return "Raise the agent's token limits or simplify the task."
    return "Run the task again; if it fails the same way, review the agent's configuration."


def _tool_suggestion(result: str) -> str:
    r = result.lower()
    if "invalid input" in r:
        return "The model sent malformed tool input. Make the tool's purpose clearer in the configuration, or add an example to the instructions."
    if "credentials not configured" in r:
        return "Set the organisation secret named in the error on the server (see Data & integrations)."
    if "sandbox" in r and "not called" in r:
        return "Expected in sandbox runs: external APIs are not called. Add a simulated response (sandbox_response) or a test fixture."
    if "internal network" in r or "cannot resolve host" in r:
        return "Check the tool's base_url: it must be a public https address."
    if "service unavailable" in r or "unreachable" in r or "timed out" in r:
        return "The external system did not respond. Check it is up; the agent should escalate when this happens."
    if "no such table" in r or "only select" in r.lower():
        return "Check which datasets the query tool may read, and that the dataset is uploaded."
    if "missing required fields" in r:
        return "The record was missing required fields. Tell the agent in its instructions which fields to fill."
    return "Review the tool's settings and the agent's instructions for this step."


def diagnose_run(store: Store, run_id: int) -> list[Finding]:
    run = store.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise KeyError(f"No run {run_id}")
    spec, _ = store.load_spec(store.agent_by_id(run["agent_id"])["name"], run["version"])
    findings: list[Finding] = []
    stop = run["stop_reason"]
    if stop == "error":
        findings.append(Finding(severity="error", title="The run failed", detail=run["error"] or "Unknown error",
                                suggestion=_error_suggestion(run["error"] or "")))
    elif stop == "max_turns":
        findings.append(Finding(severity="error", title="Ran out of steps",
                                detail=f"The agent used all {spec.limits.max_turns} model turns without finishing.",
                                suggestion="Raise 'Max model turns per run' in Configuration, or simplify the workflow."))
    elif stop == "budget_exceeded":
        findings.append(Finding(severity="error", title="Hit the token budget for one run",
                                detail=f"The run used more than {spec.limits.max_tokens_per_run:,} tokens.",
                                suggestion="Raise the token budget per run, or narrow what the agent reads (filters, fewer documents)."))
    elif stop == "max_tokens":
        findings.append(Finding(severity="error", title="The answer was cut off", detail="The model hit its output limit.",
                                suggestion="Ask for shorter answers in the instructions, or split the task."))
    elif stop == "refusal":
        findings.append(Finding(severity="error", title="The model declined the request",
                                detail="The model's safety system declined this task.",
                                suggestion="Rephrase the task. If it is legitimate work, review the instructions for wording that could be misread."))
    elif stop is None:
        findings.append(Finding(severity="info", title="Still running", detail="The run has not finished.",
                                suggestion="Wait for it to finish."))
    for e in store.run_events(run_id):
        data = json.loads(e["data_json"])
        if e["event"] == "tool_error":
            findings.append(Finding(severity="warning", title=f"Tool {data.get('tool')} returned an error",
                                    detail=str(data.get("result", ""))[:600],
                                    suggestion=_tool_suggestion(str(data.get("result", "")))))
        elif e["event"] == "tool_blocked":
            findings.append(Finding(severity="warning", title=f"Tried to use {data.get('tool')}, which it doesn't have",
                                    detail="The platform blocked a tool that is not in the agent's configuration.",
                                    suggestion="If the agent needs it, add a matching tool; otherwise make the instructions clearer."))
        elif e["event"] == "approval" and data.get("decision") in ("denied", "queued"):
            findings.append(Finding(severity="info", title=f"{data.get('tool')} {data.get('decision')}",
                                    detail="The action needed a person's approval.",
                                    suggestion="Decide queued actions in Approvals." if data.get("decision") == "queued"
                                    else "Nothing to fix: the approval rule worked."))
        elif e["event"] == "tool_repeat_skipped":
            findings.append(Finding(severity="info", title=f"{data.get('tool')} not repeated",
                                    detail="It already ran in an earlier attempt of the same job.",
                                    suggestion="Nothing to fix: duplicate actions are prevented."))
    if stop == "end_turn" and not (run["output"] or "").strip():
        findings.append(Finding(severity="warning", title="No final answer", detail="The run ended without a summary.",
                                suggestion="Ask for a final summary in the instructions."))
    if not findings:
        findings.append(Finding(severity="info", title="No problems recorded",
                                detail="The run finished normally without tool errors.",
                                suggestion="If the answer is still wrong, describe the expected result as a test case."))
    return findings


def diagnose_case(case: dict) -> list[Finding]:
    """Findings for one failed case from a test report."""
    findings = []
    if case.get("error"):
        findings.append(Finding(severity="error", title="The test run failed", detail=case["error"],
                                suggestion=_error_suggestion(case["error"])))
    for problem in [p.strip() for p in (case.get("tool_check") or "").split(";") if p.strip() and p.strip() != "ok"]:
        if problem.startswith("did not use"):
            sug = "Make the instructions say when to use this tool, or remove it from must_call if it isn't always needed."
        elif problem.startswith("attempted forbidden"):
            sug = "Tighten the instructions and restrictions so the agent doesn't try this; approval rules still blocked any real effect."
        elif problem.startswith("answer is missing"):
            sug = "The final answer lacked an expected value. Check the agent's calculation steps (use the calculate tool) and data queries."
        elif problem.startswith("answer contains forbidden"):
            sug = "The answer contained text it must never contain. Strengthen the restrictions."
        elif problem.startswith("run ended with"):
            sug = "The run did not finish normally; see the run's limits."
        else:
            sug = "Review the agent's instructions for this scenario."
        findings.append(Finding(severity="error", title=problem[0].upper() + problem[1:], detail=problem, suggestion=sug))
    if not case.get("passed") and case.get("judge_reasoning"):
        findings.append(Finding(severity="warning", title="Grader's explanation", detail=case["judge_reasoning"],
                                suggestion="Adjust the instructions so this scenario is handled as expected."))
    return findings


class FixProposal(BaseModel):
    diagnosis: str = Field(description="What went wrong and why, in plain language.")
    changes: list[str] = Field(description="Each change made to the agent, in plain language.")
    instructions: str = Field(description="The complete revised instructions, or an empty string to keep them.")
    max_turns: int = Field(description="New max turns per run, or 0 to keep the current value.")
    max_tokens_per_run: int = Field(description="New token budget per run, or 0 to keep the current value.")
    regression_test: TestCase = Field(description="A test case that reproduces this failure and passes once fixed.")


FIX_SYSTEM = """You improve AI agents that run on a platform. You get an agent's specification and \
evidence of a failure (a run or a failed test). Diagnose the cause and propose the smallest change \
that fixes it: revised instructions and, only if the evidence shows a limit was hit, new limits. You \
cannot add new tools or change permissions; if the fix needs that, say so in the diagnosis and keep \
the instructions change minimal. Always write a regression test case that reproduces the failure, \
using only the agent's own tool names; use must_include for any exact value the answer must contain."""


def propose_fix(provider, spec: AgentSpec, evidence: str, model: str | None = None) -> FixProposal:
    tools = "\n".join(f"- {t.name} ({t.component}, approval {'required' if t.requires_approval else 'not required'})"
                      for t in spec.tools)
    prompt = (f"Agent {spec.name}: {spec.purpose}\n\nInstructions:\n{spec.instructions}\n\nTools:\n{tools}\n\n"
              f"Restrictions:\n" + "\n".join(f"- {r}" for r in spec.restrictions)
              + f"\n\nLimits: max_turns={spec.limits.max_turns}, max_tokens_per_run={spec.limits.max_tokens_per_run}"
              + f"\n\nEvidence of the failure:\n{evidence[:20000]}")
    return structured(provider, FixProposal, FIX_SYSTEM, prompt, model=model or model_settings().default_model)


def apply_fix(spec: AgentSpec, proposal: FixProposal) -> AgentSpec:
    """A new spec with the proposal applied and its regression test added (validated)."""
    new = spec.model_copy(deep=True)
    if proposal.instructions.strip():
        new.instructions = proposal.instructions.strip()
    if proposal.max_turns > 0:
        new.limits.max_turns = proposal.max_turns
    if proposal.max_tokens_per_run > 0:
        new.limits.max_tokens_per_run = proposal.max_tokens_per_run
    case = proposal.regression_test.model_copy(deep=True)
    existing = {c.name for c in new.test_cases}
    base, n = case.name or "regression", 2
    while case.name in existing or not case.name:
        case.name = f"{base} ({n})"
        n += 1
    new.test_cases.append(case)
    return validate_design(new)


def add_test_case(spec: AgentSpec, case: TestCase) -> AgentSpec:
    """A new spec with `case` added. Unlike generated cases (where unknown tool names are dropped),
    a case written by a person must only name the agent's tools: a check on a missing tool would
    silently never fail (must_not_call) or never pass (must_call)."""
    new = spec.model_copy(deep=True)
    if case.name in {c.name for c in new.test_cases}:
        raise ValueError(f"A test case named {case.name!r} already exists")
    names = {t.name for t in new.tools}
    referenced = {*case.must_call, *case.must_not_call, *case.fail_tools, *(f.tool for f in case.fixtures)}
    unknown = sorted(referenced - names)
    if unknown:
        raise ValueError(f"Test case {case.name!r} refers to tools this agent doesn't have: {', '.join(unknown)}")
    new.test_cases.append(case)
    return validate_design(new)


def remove_test_case(spec: AgentSpec, name: str) -> AgentSpec:
    new = spec.model_copy(deep=True)
    before = len(new.test_cases)
    new.test_cases = [c for c in new.test_cases if c.name != name]
    if len(new.test_cases) == before:
        raise KeyError(f"No test case named {name!r}")
    return validate_design(new)
