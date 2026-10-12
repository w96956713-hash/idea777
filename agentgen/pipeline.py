"""Requirements-to-agent pipeline: intake -> discovery -> analysis -> design.

Each stage is stored on the project, can be reviewed or edited by a person, and feeds the
next. The analysis decides *whether* and *what* to automate before any agent is designed,
and states plainly which integrations exist, which can be reached through a generic API
connector, and which would need new work.
"""

import json
from typing import Literal

from pydantic import BaseModel, Field

from .components import catalog_description
from .designer import validate_design
from .llm import PRICES, DEFAULT_MODEL, structured
from .spec import AgentDesign, AgentSpec
from .store import Store

# ---------------------------------------------------------------- models


class Question(BaseModel):
    topic: str = Field(description="e.g. current process, systems, data, volumes, rules, risk, success")
    question: str
    why: str = Field(description="What the answer changes in the design.")


class Discovery(BaseModel):
    questions: list[Question]


class Opportunity(BaseModel):
    title: str
    description: str
    automatable_steps: list[str]
    human_steps: list[str] = Field(description="Steps that should stay with people, and why.")
    value: str = Field(description="Expected benefit, quantified where the information allows.")
    risk: Literal["low", "medium", "high"]
    recommended: bool


class Integration(BaseModel):
    system: str
    purpose: str
    access: Literal["read", "write"]
    status: Literal["supported", "via_http_api", "data_import", "needs_custom_connector", "unknown"] = Field(
        description="supported: a catalog component covers it; via_http_api: reachable with http_request if the "
        "customer has an API and credentials; data_import: export to CSV and import; needs_custom_connector: no "
        "way yet; unknown: not enough information."
    )
    approach: str


class Analysis(BaseModel):
    process_summary: str
    opportunities: list[Opportunity]
    recommended_agent: str = Field(description="What agent to build and why, possibly one the customer didn't ask for.")
    recommended_capabilities: list[str]
    keep_human: list[str] = Field(description="Decisions or actions that should not be automated, with reasons.")
    integrations: list[Integration]
    data_needed: list[str]
    risks: list[str]
    complexity: Literal["low", "medium", "high"]
    complexity_reasons: list[str]
    estimated_runs_per_month: int
    open_questions: list[str]


# Rough per-run token use by complexity, for estimates before any test has run.
_TOKENS_PER_RUN = {"low": (30_000, 3_000), "medium": (80_000, 6_000), "high": (200_000, 12_000)}


def estimate_monthly_cost(analysis: Analysis, model: str = DEFAULT_MODEL) -> dict:
    tokens_in, tokens_out = _TOKENS_PER_RUN[analysis.complexity]
    price_in, price_out = PRICES.get(model, PRICES[DEFAULT_MODEL])
    per_run = (tokens_in * price_in + tokens_out * price_out) / 1_000_000
    return {
        "model": model,
        "assumed_tokens_per_run": {"input": tokens_in, "output": tokens_out},
        "usd_per_run": round(per_run, 3),
        "usd_per_month": round(per_run * analysis.estimated_runs_per_month, 2),
        "basis": "rough estimate from complexity; replaced by measured cost after testing",
    }


# ---------------------------------------------------------------- prompts

_CONTEXT_RULES = """You work for a company that builds AI agents for businesses. You are careful and \
concrete: you use the customer's own terms, you don't invent facts about their business, and you say \
what is unknown. Prefer the simplest solution that delivers real value, and keep people in charge of \
consequential decisions."""

DISCOVERY_SYSTEM = _CONTEXT_RULES + """

Task: read a company's description of a problem and write the questions a senior automation \
consultant would ask before designing anything. Ask about: how the process works today step by step, \
volumes and timing, which systems hold the data and whether they have APIs or exports, who decides \
what, rules and exceptions, what must never go wrong, permissions and privacy constraints, and how \
success will be measured. Skip anything the description already answers. 6-12 questions, most \
important first."""

ANALYSIS_SYSTEM = _CONTEXT_RULES + """

Task: analyse the company's process and decide what to automate with an AI agent.
- Identify opportunities, splitting each into steps an agent can do safely and steps that should stay \
human. Recommend the ones with clear value and manageable risk. You may recommend an agent the \
customer did not ask for if it would serve them better.
- For each system involved, say how the agent would reach it, using the platform's components below. \
Be honest: if nothing reaches it, mark it needs_custom_connector; if you don't know whether they have \
an API, mark it unknown.
- Estimate monthly run volume from the information given (state it in open_questions if it is a guess).

Platform components:
{catalog}"""

DESIGN_SYSTEM = _CONTEXT_RULES + """

Task: design one agent as a complete specification. The agent is assembled from these components only:
{catalog}

Design rules:
- Tools: one entry per tool the agent sees. Give each a clear snake_case name and a purpose. Configure \
components through config keys only. Use http_request for a customer API only when the analysis says \
one exists; put credentials only as a secret name in auth_env; give params_schema as a JSON string. \
Choose the fewest tools that do the job.
- Approval: tools that change records need requires_approval=true unless the requirements clearly \
accept automatic changes; external communication, external writes and money always need approval.
- Data: list every data source with its real status. Never pretend a source is available.
- Workflow: the steps the agent follows, each with what to do on failure (retry once, fallback, or \
escalate). Include branching conditions in the descriptions.
- Triggers: manual, schedule (interval_minutes) and/or event (event name, input_template with {{payload}}).
- Memory: enable only if remembering between runs is needed; say exactly what may be stored.
- Limits: realistic max_turns and max_tokens_per_run for the task.
- Instructions: addressed to the agent ("You are..."); cover role, workflow, tone/language, decision \
rules, when to escalate, what never to do.
- Tests: 8-14 cases across the categories: task, quality, tool_use, missing_info, permission, \
unauthorized_action, api_failure (use fail_tools), edge_case. Include at least one attempt to make the \
agent break its restrictions through instructions hidden in the input. For tools that reach external \
systems, give fixtures with realistic responses. must_call/must_not_call/fail_tools/fixtures use tool \
names. Only put a tool in must_call when every correct answer must use it.
- assumptions: what you assumed where information was missing. open_questions: what must be \
confirmed with the customer, including missing integrations."""


# ---------------------------------------------------------------- stages

INTAKE_FIELDS = [
    ("company", "Company"),
    ("industry", "Industry"),
    ("problem", "Problem to solve"),
    ("workflow", "Current workflow"),
    ("outcome", "Desired outcome"),
    ("systems", "Existing software and data sources"),
    ("constraints", "Operational and security constraints"),
]


def compose_intake(fields: dict) -> str:
    """The structured intake form as one description for the models."""
    return "\n\n".join(f"{label}:\n{fields[key].strip()}" for key, label in INTAKE_FIELDS
                         if str(fields.get(key, "")).strip())


def _qa(project) -> list[dict]:
    return json.loads(project["answers_json"]) if project["answers_json"] else []


def generate_questions(client, store: Store, project_id: int) -> Discovery:
    project = store.project(project_id)
    discovery = structured(client, Discovery, DISCOVERY_SYSTEM, _context(store, project))
    store.update_project(project_id, questions_json=discovery.model_dump_json(), stage="discovery")
    return discovery


def record_answers(store: Store, project_id: int, answers: list[dict]) -> None:
    """answers: [{"question": ..., "answer": ...}]. Unanswered questions are kept with an empty answer."""
    cleaned = [{"question": a["question"], "answer": str(a.get("answer", "")).strip()} for a in answers]
    store.update_project(project_id, answers_json=json.dumps(cleaned, ensure_ascii=False))


def _available_data(store: Store, customer_id: int) -> str:
    lines = []
    for ds in store.datasets(customer_id):
        lines.append(f"- dataset `{ds['name']}` ({ds['row_count']} rows): {', '.join(json.loads(ds['columns_json']))}")
    for doc in store.documents(customer_id):
        lines.append(f"- document \"{doc['title']}\" ({doc['size']} characters)")
    if not lines:
        return "No datasets or documents have been uploaded to the platform yet."
    return ("Already uploaded to the platform by this company (use these exact dataset names in query_data "
            "config; documents are searchable with knowledge_search):\n" + "\n".join(lines))


def _context(store: Store, project) -> str:
    parts = [f"Company description:\n{project['intake']}", _available_data(store, project["customer_id"])]
    qa = [a for a in _qa(project) if a["answer"]]
    if qa:
        parts.append("Discovery answers:\n" + "\n".join(f"Q: {a['question']}\nA: {a['answer']}" for a in qa))
    answered = {a["question"] for a in _qa(project) if a["answer"]}
    asked = [q["question"] for q in json.loads(project["questions_json"])["questions"]] if project["questions_json"] else []
    unanswered = [q for q in asked if q not in answered] + [
        a["question"] for a in _qa(project) if not a["answer"] and a["question"] not in asked]
    if unanswered:
        parts.append("Unanswered questions (treat as unknown):\n" + "\n".join(f"- {q}" for q in unanswered))
    if project["amendments"]:
        parts.append("Corrections and decisions from the customer's review (these override anything above):\n"
                     + project["amendments"])
    return "\n\n".join(parts)


def analyze(client, store: Store, project_id: int) -> Analysis:
    project = store.project(project_id)
    analysis = structured(client, Analysis, ANALYSIS_SYSTEM.format(catalog=catalog_description()),
                          _context(store, project))
    store.update_project(project_id, analysis_json=analysis.model_dump_json(), stage="analysed")
    return analysis


def design(client, store: Store, project_id: int) -> tuple[AgentSpec, int]:
    project = store.project(project_id)
    prompt = _context(store, project)
    if project["analysis_json"]:
        prompt += "\n\nAnalysis (design the recommended agent):\n" + project["analysis_json"]
    design_out = structured(client, AgentDesign, DESIGN_SYSTEM.format(catalog=catalog_description()), prompt)
    spec = validate_design(AgentSpec(**design_out.model_dump()))
    if project["agent_name"]:
        spec.name = project["agent_name"]  # later designs of the same project become new versions
    version = store.save_version(project["customer_id"], spec)
    store.update_project(project_id, agent_name=spec.name, stage="designed")
    return spec, version
