"""Validation and assembly: enforce platform policy on whatever a designer (model or person)
produced, so a bad generation can't slip into a runnable agent.

Rules applied:
- only catalog components; tool names unique and valid; component config validated;
- tools that write externally, communicate externally or move money always need approval;
- escalate_to_human always present and never gated;
- memory tools only when the memory policy is enabled, with a retention period;
- limits and triggers sane;
- test cases only reference tools the agent has; missing safety/failure coverage is reported.
Everything changed or missing is recorded in open_questions so a person sees it.
"""

import re

from .components import ALWAYS_APPROVE, CATALOG, risk_of
from .spec import SAFETY_CATEGORIES, AgentSpec, ToolSpec

_TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def validate_design(spec: AgentSpec) -> AgentSpec:
    notes: list[str] = []
    tools: list[ToolSpec] = []
    names: set[str] = set()

    for tool in spec.tools:
        component = CATALOG.get(tool.component)
        if component is None:
            notes.append(f"Tool {tool.name!r} needs component {tool.component!r}, which doesn't exist; removed. "
                         "A connector must be built or the step done by a person.")
            continue
        if not _TOOL_NAME.match(tool.name) or tool.name in names:
            base = re.sub(r"[^a-zA-Z0-9_-]+", "_", tool.name)[:56] or tool.component
            new = base
            i = 2
            while new in names:
                new, i = f"{base}_{i}", i + 1
            tool.name = new
        known = component.config_keys
        dropped = [c.key for c in tool.config if c.key not in known]
        if dropped:
            tool.config = [c for c in tool.config if c.key in known]
            notes.append(f"Tool {tool.name!r}: ignored unknown config {', '.join(dropped)}.")
        problems = component.validate(tool)
        if problems:
            notes.append(f"Tool {tool.name!r} removed until fixed: {'; '.join(problems)}.")
            continue
        if tool.component in ("remember", "recall") and not spec.memory.enabled:
            notes.append(f"Tool {tool.name!r} removed because memory is disabled.")
            continue
        if risk_of(tool) in ALWAYS_APPROVE and not tool.requires_approval:
            tool.requires_approval = True
            notes.append(f"Tool {tool.name!r} ({risk_of(tool)}) now requires approval; this can't be turned off.")
        if tool.component == "escalate_to_human":
            tool.requires_approval = False
        names.add(tool.name)
        tools.append(tool)

    if not any(t.component == "escalate_to_human" for t in tools):
        name = "escalate_to_human" if "escalate_to_human" not in names else "escalate_to_person"
        tools.append(ToolSpec(name=name, component="escalate_to_human", requires_approval=False, config=[],
                              purpose="When you can't complete the task safely."))
        names.add(name)
    spec.tools = tools

    if spec.memory.enabled and spec.memory.retention_days <= 0:
        spec.memory.retention_days = 30
        spec.assumptions.append("Memory retention set to 30 days because none was given.")
    spec.limits.max_turns = min(max(spec.limits.max_turns, 3), 50)
    spec.limits.max_tokens_per_run = min(max(spec.limits.max_tokens_per_run, 20000), 1_000_000)

    triggers = []
    for t in spec.triggers:
        if t.kind == "schedule" and t.interval_minutes < 5:
            notes.append(f"Schedule trigger {t.description!r} removed: interval must be at least 5 minutes.")
        elif t.kind == "event" and not t.event.strip():
            notes.append(f"Event trigger {t.description!r} removed: it has no event name.")
        else:
            triggers.append(t)
    spec.triggers = triggers

    for case in spec.test_cases:
        case.must_call = [n for n in case.must_call if n in names]
        case.must_not_call = [n for n in case.must_not_call if n in names]
        case.fail_tools = [n for n in case.fail_tools if n in names]
        case.fixtures = [f for f in case.fixtures if f.tool in names]

    categories = {c.category for c in spec.test_cases}
    if not categories & SAFETY_CATEGORIES:
        notes.append("Tests are missing a permission or unauthorised-action case; add one before release.")
    external = [t.name for t in tools if t.component == "http_request"]
    if external and "api_failure" not in categories:
        notes.append(f"Tests are missing an api_failure case for {', '.join(external)}.")
    for name in external:
        if not any(f.tool == name for c in spec.test_cases for f in c.fixtures) and not any(
                name in c.fail_tools for c in spec.test_cases):
            notes.append(f"No test provides a fixture for {name!r}, so tests can't exercise it.")

    spec.open_questions = list(dict.fromkeys(spec.open_questions + notes))
    return spec
