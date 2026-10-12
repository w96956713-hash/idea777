"""Reusable, tested building blocks that generated agents are assembled from.

Each component becomes one tool the agent can call. The designer may only pick
components from this catalog, so every agent is made of code that has been reviewed.
Adding an integration (Fortnox, Visma, Outlook, a CRM...) means adding one class here.
"""

import json
import re
from dataclasses import dataclass

from .store import Store


@dataclass
class Context:
    store: Store
    agent_id: int
    config: dict[str, str]


class Component:
    name: str
    description: str
    input_schema: dict
    sensitive: bool = False  # sensitive components default to requiring human approval
    config_keys: dict[str, str] = {}

    def tool_definition(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "strict": True,
        }

    def run(self, args: dict, ctx: Context) -> str:
        raise NotImplementedError


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required if required is not None else list(properties),
        "additionalProperties": False,
    }


_WORD = re.compile(r"\w+", re.UNICODE)


class SearchKnowledge(Component):
    name = "search_knowledge"
    description = (
        "Search the company's knowledge base (policies, price lists, FAQs, procedures). "
        "Returns the most relevant passages with their document titles. Use this before "
        "answering any question about the company's rules or offerings."
    )
    input_schema = _schema({"query": {"type": "string", "description": "What to look for."}})
    config_keys = {"max_results": "Number of passages to return (default 3)."}

    def run(self, args, ctx):
        terms = {w.lower() for w in _WORD.findall(args["query"]) if len(w) > 2}
        scored = []
        for doc in ctx.store.knowledge(ctx.agent_id):
            for para in [p.strip() for p in doc["content"].split("\n\n") if p.strip()]:
                words = [w.lower() for w in _WORD.findall(para)]
                score = sum(words.count(t) for t in terms) + 2 * sum(t in doc["title"].lower() for t in terms)
                if score:
                    scored.append((score, doc["title"], para))
        scored.sort(key=lambda s: -s[0])
        limit = int(ctx.config.get("max_results", "3"))
        if not scored:
            return "No matching passages found in the knowledge base."
        return "\n\n".join(f"[{title}]\n{para}" for _, title, para in scored[:limit])


class DraftEmail(Component):
    name = "draft_email"
    description = (
        "Save an email draft for a human to review and send. Does not send anything. "
        "Use this when a reply should be prepared but a person will send it."
    )
    input_schema = _schema(
        {
            "to": {"type": "string", "description": "Recipient email address."},
            "subject": {"type": "string"},
            "body": {"type": "string"},
        }
    )

    def run(self, args, ctx):
        draft_id = ctx.store.add_outbox(ctx.agent_id, "draft", args["to"], args["subject"], args["body"])
        return f"Draft saved (id {draft_id}) for human review."


class SendEmail(Component):
    name = "send_email"
    description = "Send an email to a recipient. This is an external action and may require human approval."
    input_schema = DraftEmail.input_schema
    sensitive = True
    config_keys = {"allowed_domains": "Comma-separated recipient domains the agent may email."}

    def run(self, args, ctx):
        allowed = [d.strip().lower() for d in ctx.config.get("allowed_domains", "").split(",") if d.strip()]
        domain = args["to"].rsplit("@", 1)[-1].lower()
        if allowed and domain not in allowed:
            return f"Refused: {domain} is not an allowed recipient domain."
        # Delivery is recorded in the outbox; a mail connector (SMTP, Microsoft Graph,
        # Gmail API) reads 'sent' rows and delivers them.
        msg_id = ctx.store.add_outbox(ctx.agent_id, "sent", args["to"], args["subject"], args["body"])
        return f"Email queued for delivery (id {msg_id})."


class CreateRecord(Component):
    name = "create_record"
    description = (
        "Create a record in the company's system (e.g. a case, task, or ticket). "
        "Provide the fields as a JSON object encoded in a string."
    )
    input_schema = _schema(
        {"fields_json": {"type": "string", "description": 'JSON object of field names to values, e.g. {"title": "..."}'}}
    )
    sensitive = True
    config_keys = {"table": "Name of the table/record type (default 'cases')."}

    def run(self, args, ctx):
        try:
            data = json.loads(args["fields_json"])
        except json.JSONDecodeError as e:
            return f"Error: fields_json is not valid JSON ({e})."
        if not isinstance(data, dict):
            return "Error: fields_json must be a JSON object."
        record_id = ctx.store.add_record(ctx.agent_id, ctx.config.get("table", "cases"), data)
        return f"Record created (id {record_id})."


class EscalateToHuman(Component):
    name = "escalate_to_human"
    description = (
        "Hand the task to a human colleague. Use when the request is outside your instructions, "
        "is ambiguous, involves a complaint or legal/financial risk, or you are not confident."
    )
    input_schema = _schema(
        {
            "reason": {"type": "string", "description": "Why a human is needed."},
            "details": {"type": "string", "description": "Summary of the case for the human."},
        }
    )

    def run(self, args, ctx):
        esc_id = ctx.store.add_escalation(ctx.agent_id, args["reason"], args["details"])
        return f"Escalated to a human (id {esc_id})."


CATALOG: dict[str, Component] = {
    c.name: c for c in [SearchKnowledge(), DraftEmail(), SendEmail(), CreateRecord(), EscalateToHuman()]
}


def catalog_description() -> str:
    """Human- and model-readable list of available components."""
    lines = []
    for c in CATALOG.values():
        line = f"- {c.name}{' (sensitive)' if c.sensitive else ''}: {c.description}"
        for key, desc in c.config_keys.items():
            line += f"\n    config `{key}`: {desc}"
        lines.append(line)
    return "\n".join(lines)
