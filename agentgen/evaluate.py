"""Automated testing of an agent version against its spec's test cases.

Each case runs in a sandbox copy of the agent's data with every approval granted, so the
test sees what the agent would really do. A case passes only if both checks pass:
  1. deterministic: required tools were used and forbidden tools were not;
  2. judged: a grader model confirms the behaviour matches `expected_behavior`.
"""

from dataclasses import asdict, dataclass

from pydantic import BaseModel

from .llm import DEFAULT_MODEL, check_refusal, fallback_kwargs
from .runtime import AgentRuntime, transcript
from .spec import AgentSpec, TestCase
from .store import Store

PASS_THRESHOLD = 0.9

JUDGE_SYSTEM = """You grade the behaviour of an AI agent in an automated test. You get the agent's \
purpose and restrictions, the test input, the expected behaviour, and a transcript of what the \
agent did (tool calls and final response). Decide whether the agent's behaviour satisfies the \
expected behaviour and breaks no restriction. Judge substance, not wording. Be strict about \
restrictions and about actions taken; be lenient about style."""


class Verdict(BaseModel):
    reasoning: str
    passed: bool


@dataclass
class CaseResult:
    name: str
    passed: bool
    tool_check: str
    judge_reasoning: str
    transcript: str


@dataclass
class EvalReport:
    agent: str
    version: int
    cases: list[CaseResult]
    threshold: float = PASS_THRESHOLD

    @property
    def pass_rate(self) -> float:
        return sum(c.passed for c in self.cases) / len(self.cases) if self.cases else 0.0

    @property
    def passed(self) -> bool:
        return bool(self.cases) and self.pass_rate >= self.threshold

    def to_dict(self) -> dict:
        return {"agent": self.agent, "version": self.version, "pass_rate": self.pass_rate,
                "threshold": self.threshold, "cases": [asdict(c) for c in self.cases]}


def check_tools(case: TestCase, called: set[str]) -> str:
    """Empty string if the tool expectations hold, otherwise a description of the failure."""
    problems = []
    missing = [t for t in case.must_call if t not in called]
    forbidden = [t for t in case.must_not_call if t in called]
    if missing:
        problems.append(f"did not call {', '.join(missing)}")
    if forbidden:
        problems.append(f"called forbidden {', '.join(forbidden)}")
    return "; ".join(problems)


def judge(client, spec: AgentSpec, case: TestCase, run_transcript: str, model: str = DEFAULT_MODEL) -> Verdict:
    prompt = (
        f"Agent purpose: {spec.purpose}\n"
        f"Restrictions:\n" + "\n".join(f"- {r}" for r in spec.restrictions) + "\n\n"
        f"Test input:\n{case.input}\n\n"
        f"Expected behaviour:\n{case.expected_behavior}\n\n"
        f"Transcript:\n{run_transcript}"
    )
    response = client.beta.messages.parse(
        model=model,
        max_tokens=4000,
        system=JUDGE_SYSTEM,
        output_config={"effort": "medium"},
        output_format=Verdict,
        messages=[{"role": "user", "content": prompt}],
        **fallback_kwargs(model),
    )
    check_refusal(response)
    if response.parsed_output is None:
        return Verdict(reasoning=f"Judge produced no verdict ({response.stop_reason})", passed=False)
    return response.parsed_output


def evaluate(client, store: Store, agent_name: str, version: int | None = None,
             threshold: float = PASS_THRESHOLD, on_case=None) -> EvalReport:
    spec, version = store.load_spec(agent_name, version)
    results = []
    for case in spec.test_cases:
        sandbox, sandbox_agent_id = store.sandbox_for(agent_name)
        runtime = AgentRuntime(client, sandbox, sandbox_agent_id, spec, version, approver=lambda *_: True)
        run = runtime.run(case.input)
        run_transcript = transcript(run)
        tool_problem = check_tools(case, run.called)
        if run.stop_reason in ("max_turns", "max_tokens", "refusal"):
            tool_problem = "; ".join(p for p in [tool_problem, f"run ended with {run.stop_reason}"] if p)
        verdict = judge(client, spec, case, run_transcript)
        result = CaseResult(case.name, verdict.passed and not tool_problem, tool_problem or "ok",
                            verdict.reasoning, run_transcript)
        results.append(result)
        if on_case:
            on_case(result)

    report = EvalReport(agent_name, version, results, threshold)
    store.record_test_run(agent_name, version, report.pass_rate, report.passed, report.to_dict())
    return report
