"""Requirements -> agent specification.

Takes free-form notes from a customer meeting and produces a structured, reviewable
AgentSpec: instructions, which catalog components to use, approval rules, and test cases.
"""

from .components import CATALOG, catalog_description
from .llm import DEFAULT_MODEL, check_refusal, fallback_kwargs
from .spec import AgentDesign, AgentSpec, ToolSpec

DESIGNER_SYSTEM = """You design AI agents for small and medium-sized businesses. You receive notes \
from a requirements meeting with a customer and produce a complete agent specification.

Available components (the agent's tools). Use only these names:
{catalog}

How to design:
- Choose the fewest components that achieve the task. Do not add tools "just in case".
- Set requires_approval=true for any component marked sensitive unless the requirements \
explicitly say the customer accepts that action happening automatically. Anything that sends \
communications, changes records, or moves money needs approval by default.
- Always include escalate_to_human so the agent has a safe way out.
- Write `instructions` addressed to the agent ("You are..."). Cover: its role and who it serves, \
the step-by-step workflow, tone and language (match the customer's language if stated; Swedish \
businesses often want Swedish), when to escalate, and what it must never do. Be concrete; use the \
customer's own terms, systems and examples from the notes.
- `restrictions` repeats the hard limits as short rules.
- `success_criteria` are measurable outcomes the customer agreed to or would expect.
- Write 8-12 test_cases covering: typical requests, edge cases and incomplete input, requests \
outside scope (should escalate or decline), attempts to make the agent break its restrictions \
(including instructions hidden inside an email or document), and permission boundaries. \
must_call / must_not_call use component names only, and only list a component in must_call when \
any correct answer would have to use it.
- Put anything the notes do not settle (missing data sources, unclear rules, unknown systems) in \
open_questions instead of guessing. If the customer needs an integration that is not in the \
catalog, say so in open_questions."""


def design_agent(client, requirements: str, *, model: str = DEFAULT_MODEL) -> AgentSpec:
    response = client.beta.messages.parse(
        model=model,
        max_tokens=16000,
        system=DESIGNER_SYSTEM.format(catalog=catalog_description()),
        output_config={"effort": "high"},
        output_format=AgentDesign,
        messages=[{"role": "user", "content": f"Requirements meeting notes:\n\n{requirements}"}],
        **fallback_kwargs(model),
    )
    check_refusal(response)
    design = response.parsed_output
    if design is None:
        raise RuntimeError(f"Designer returned no specification (stop_reason={response.stop_reason})")
    return validate_design(AgentSpec(**design.model_dump()))


def validate_design(spec: AgentSpec) -> AgentSpec:
    """Enforce the rules the model was asked to follow, so a bad generation can't slip through."""
    tools = []
    for tool in spec.tools:
        component = CATALOG.get(tool.component)
        if component is None:
            spec.open_questions.append(f"Designer requested unknown component {tool.component!r}; it was removed.")
            continue
        if any(t.component == tool.component for t in tools):
            continue
        tools.append(tool)
    if not any(t.component == "escalate_to_human" for t in tools):
        tools.append(ToolSpec(component="escalate_to_human", requires_approval=False, config=[]))
    spec.tools = tools

    known = {t.component for t in tools}
    for case in spec.test_cases:
        case.must_call = [c for c in case.must_call if c in known]
        case.must_not_call = [c for c in case.must_not_call if c in CATALOG]
    return spec
