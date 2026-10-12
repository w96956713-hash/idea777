"""Command-line interface.

Requirements to agent:
  agentgen project new intake.md --customer "Acme AB" --title "Request routing"   # asks discovery questions
  agentgen project answer 1                     # answer interactively (or --file answers.yaml)
  agentgen project analyze 1                    # opportunities, integrations, risks, cost
  agentgen project design 1                     # agent spec v1 (stored, status draft)
  agentgen design notes.md --customer "Acme AB"  # shortcut: notes straight to a spec

Data and knowledge:
  agentgen data import "Acme AB" tenants tenants.csv
  agentgen knowledge AGENT docs/*.md

Review, test, release:
  agentgen export AGENT -o spec.yaml ; agentgen import spec.yaml --customer "Acme AB"
  agentgen test AGENT
  agentgen approve AGENT --version 2 --by "Karin Andersson"
  agentgen deploy AGENT [--version N]           # older passed version = rollback

Operate:
  agentgen chat AGENT                           # interactive; approvals asked in the terminal
  agentgen trigger AGENT new_request --payload '{"text": "..."}'
  agentgen worker [--once]                      # runs schedules, events and follow-ups
  agentgen actions list | approve ID --by NAME | deny ID --by NAME
  agentgen monitor AGENT [--days 30]
"""

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

from .components import catalog_description, is_consequential, risk_of
from .llm import make_client
from .spec import AgentSpec
from .store import Store


def _store(args) -> Store:
    return Store(args.db)


def _err(*lines):
    for line in lines:
        print(line, file=sys.stderr)


# ---------------------------------------------------------------- projects

def cmd_project_new(args):
    from .pipeline import generate_questions

    store = _store(args)
    intake = Path(args.intake).read_text(encoding="utf-8")
    customer_id = store.get_or_create_customer(args.customer)
    project_id = store.create_project(customer_id, args.title or Path(args.intake).stem, intake)
    print(f"Project #{project_id} created. Generating discovery questions...", file=sys.stderr)
    discovery = generate_questions(make_client(), store, project_id)
    for i, q in enumerate(discovery.questions, 1):
        print(f"{i}. [{q.topic}] {q.question}\n   why: {q.why}")
    _err(f"\nAnswer with: agentgen project answer {project_id}")


def cmd_project_answer(args):
    from .pipeline import record_answers

    store = _store(args)
    project = store.project(args.id)
    questions = json.loads(project["questions_json"] or '{"questions": []}')["questions"]
    if args.file:
        given = yaml.safe_load(Path(args.file).read_text(encoding="utf-8")) or []
        answers = [{"question": a["question"], "answer": a.get("answer", "")} for a in given]
    else:
        if not questions:
            sys.exit("This project has no discovery questions yet")
        answers = []
        _err("Answer each question (empty line to skip).")
        for i, q in enumerate(questions, 1):
            print(f"\n{i}. {q['question']}")
            answers.append({"question": q["question"], "answer": input("> ")})
    record_answers(store, args.id, answers)
    print(f"Saved {sum(1 for a in answers if a['answer'].strip())} answers. Next: agentgen project analyze {args.id}")


def cmd_project_analyze(args):
    from .pipeline import analyze, estimate_monthly_cost

    store = _store(args)
    _err("Analysing...")
    a = analyze(make_client(), store, args.id)
    print(f"PROCESS\n  {a.process_summary}\n\nRECOMMENDED AGENT\n  {a.recommended_agent}")
    for cap in a.recommended_capabilities:
        print(f"  - {cap}")
    print("\nOPPORTUNITIES")
    for o in a.opportunities:
        mark = "recommended" if o.recommended else "not now"
        print(f"  * {o.title} [{o.risk} risk, {mark}]: {o.value}")
    print("\nKEEP HUMAN")
    for k in a.keep_human:
        print(f"  - {k}")
    print("\nINTEGRATIONS")
    for i in a.integrations:
        print(f"  - {i.system} ({i.access}): {i.status} - {i.approach}")
    print(f"\nCOMPLEXITY  {a.complexity}: " + "; ".join(a.complexity_reasons))
    est = estimate_monthly_cost(a)
    print(f"ESTIMATED MODEL COST  ~${est['usd_per_month']}/month at {a.estimated_runs_per_month} runs "
          f"(${est['usd_per_run']}/run, {est['basis']})")
    if a.risks:
        print("\nRISKS\n" + "\n".join(f"  - {r}" for r in a.risks))
    if a.open_questions:
        print("\nOPEN QUESTIONS\n" + "\n".join(f"  - {q}" for q in a.open_questions))
    _err(f"\nNext: agentgen project design {args.id}")


def _print_design_summary(spec: AgentSpec, version: int):
    print(spec.to_yaml())
    _err(f"Saved {spec.name} v{version} (status: draft).")
    if spec.open_questions:
        _err("\nOpen questions and validation notes:", *[f"  - {q}" for q in spec.open_questions])


def cmd_project_design(args):
    from .pipeline import design

    _err("Designing agent...")
    spec, version = design(make_client(), _store(args), args.id)
    _print_design_summary(spec, version)


def cmd_project_show(args):
    store = _store(args)
    p = store.project(args.id)
    print(f"Project #{p['id']}: {p['title']}  stage: {p['stage']}  agent: {p['agent_name'] or '-'}\n")
    print(p["intake"])
    for a in json.loads(p["answers_json"] or "[]"):
        print(f"\nQ: {a['question']}\nA: {a['answer'] or '(unanswered)'}")
    if p["analysis_json"]:
        print("\nAnalysis:\n" + json.dumps(json.loads(p["analysis_json"]), indent=2, ensure_ascii=False))


def cmd_projects(args):
    for p in _store(args).list_projects():
        print(f"#{p['id']:<4} {p['title']:<36} {p['customer']:<28} {p['stage']:<10} {p['agent_name'] or ''}")


def cmd_design(args):
    """Shortcut: requirements notes straight to a spec, skipping discovery."""
    from .pipeline import analyze, design

    store = _store(args)
    notes = Path(args.requirements).read_text(encoding="utf-8")
    customer_id = store.get_or_create_customer(args.customer)
    project_id = store.create_project(customer_id, Path(args.requirements).stem, notes)
    client = make_client()
    if args.analyze:
        _err("Analysing...")
        analyze(client, store, project_id)
    _err("Designing agent...")
    spec, version = design(client, store, project_id)
    _print_design_summary(spec, version)


# ---------------------------------------------------------------- specs, data, knowledge

def cmd_export(args):
    spec, version = _store(args).load_spec(args.agent, args.version)
    text = spec.to_yaml()
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        _err(f"Wrote {args.agent} v{version} to {args.output}")
    else:
        print(text)


def cmd_import(args):
    from .designer import validate_design

    store = _store(args)
    spec = validate_design(AgentSpec.from_yaml(Path(args.file).read_text(encoding="utf-8")))
    version = store.save_version(store.get_or_create_customer(args.customer), spec)
    print(f"Saved {spec.name} v{version} (status: draft).")
    if spec.open_questions:
        _err("Open questions and validation notes:", *[f"  - {q}" for q in spec.open_questions])


def cmd_knowledge(args):
    store = _store(args)
    agent_id = store.agent(args.agent)["id"]
    for f in args.files:
        path = Path(f)
        store.add_knowledge(agent_id, path.stem, path.read_text(encoding="utf-8"))
        print(f"Added {path.name}")


def cmd_data_import(args):
    from .datasets import import_csv

    store = _store(args)
    columns, rows = import_csv(store, store.get_or_create_customer(args.customer), args.name, args.file)
    print(f"Imported {rows} rows into {args.name} ({', '.join(columns)})")


def cmd_data_list(args):
    store = _store(args)
    for ds in store.datasets(store.get_or_create_customer(args.customer)):
        print(f"{ds['name']:<24} {ds['row_count']:>7} rows  {', '.join(json.loads(ds['columns_json']))}")


# ---------------------------------------------------------------- test & release

def cmd_test(args):
    from .evaluate import evaluate

    def show(case):
        mark = "PASS" if case.passed else "FAIL"
        tag = " (regression suite)" if case.carried else ""
        print(f"[{mark}] {case.category:<19} {case.name}{tag}")
        if not case.passed:
            print(f"       tools: {case.tool_check}")
            print(f"       grader: {case.judge_reasoning}")
        if args.verbose:
            print("       " + case.transcript.replace("\n", "\n       "))

    report = evaluate(make_client(), _store(args), args.agent, args.version, args.threshold, on_case=show)
    print(f"\n{args.agent} v{report.version}: {report.pass_rate:.0%} of {len(report.cases)} cases passed "
          f"(threshold {report.threshold:.0%})")
    if report.avg_usd_per_run is not None:
        print(f"Average model cost per run: ${report.avg_usd_per_run:.4f}")
    if report.over_budget:
        print("Over the token budget: " + ", ".join(report.over_budget))
    if report.skipped_carried:
        print("Regression cases not carried (tools removed): " + ", ".join(report.skipped_carried))
    if report.passed:
        print("PASSED")
    else:
        print("FAILED: " + "; ".join(report.blockers()))
    sys.exit(0 if report.passed else 1)


def cmd_approve(args):
    store = _store(args)
    version = args.version or store.load_spec(args.agent)[1]
    store.approve_version(args.agent, version, args.by, args.note or "")
    print(f"{args.agent} v{version} approved by {args.by}.")


def cmd_deploy(args):
    store = _store(args)
    spec, version = store.load_spec(args.agent, args.version)
    store.deploy(args.agent, version, requires_approval=is_consequential(spec.tools))
    print(f"{args.agent} now runs v{version}.")


# ---------------------------------------------------------------- operate

def _terminal_approver(tool: str, tool_input: dict) -> bool:
    print(f"\n  Approval needed: {tool}")
    for k, v in tool_input.items():
        print(f"    {k}: {v}")
    return input("  Approve? [y/N] ").strip().lower() in ("y", "yes", "j", "ja")


def cmd_chat(args):
    from .runtime import AgentRuntime

    store = _store(args)
    spec, version = store.deployed_spec(args.agent)
    runtime = AgentRuntime(make_client(), store, store.agent(args.agent)["id"], spec, version,
                           approver=_terminal_approver)
    if args.message:
        print(runtime.run(args.message).text)
        return
    print(f"Chatting with {args.agent} v{version}. Ctrl-D to quit.")
    history: list = []
    while True:
        try:
            msg = input("\nyou> ")
        except EOFError:
            break
        result = runtime.run(msg, history)
        history = result.messages
        print(f"\n{args.agent}> {result.text}")


def cmd_trigger(args):
    from .worker import fire_event, run_due_jobs

    store = _store(args)
    payload = Path(args.payload[1:]).read_text(encoding="utf-8") if args.payload.startswith("@") else args.payload
    jobs = fire_event(store, args.agent, args.event, payload)
    print(f"Queued job(s) {', '.join(map(str, jobs))}.")
    if args.run_now:
        for outcome in run_due_jobs(make_client(), store):
            print(outcome)


def cmd_worker(args):
    from .worker import work

    work(make_client(), _store(args), once=args.once, poll_seconds=args.poll)


def cmd_actions_list(args):
    for a in _store(args).pending_actions():
        print(f"#{a['id']:<5} {a['agent']:<28} {a['tool']:<24} {a['input_json']}")


def cmd_actions_decide(args, approve: bool):
    from .runtime import execute_queued_action

    print(execute_queued_action(_store(args), args.id, args.by, approve))


def cmd_monitor(args):
    from .monitor import agent_metrics

    m = agent_metrics(_store(args), args.agent, args.days)
    pct = lambda v: "-" if v is None else f"{v:.0%}"
    print(f"{m['agent']}  deployed v{m['deployed_version']}  last {m['days']} days")
    print(f"  runs {m['runs']}  completed {pct(m['completion_rate'])}  escalated {pct(m['escalation_rate'])} "
          f"({m['escalations']})")
    print(f"  tool calls {m['tool_calls']}  tool errors {pct(m['tool_error_rate'])}  blocked {m['blocked_tool_calls']}")
    print(f"  approvals {m['approvals'] or '-'}  awaiting decision {m['pending_actions']}  failed jobs {m['failed_jobs']}")
    print(f"  tokens in/out {m['input_tokens']}/{m['output_tokens']}  cost ${m['cost_usd']} "
          f"(${m['cost_usd_per_run'] or 0}/run; {m['cost_note']})")
    for v, s in m["by_version"].items():
        print(f"  v{v}: {s['runs']} runs, {s['completion_rate']:.0%} completed")
    if m["stop_reasons"]:
        print(f"  stop reasons: {m['stop_reasons']}")


def cmd_list(args):
    for row in _store(args).list_agents():
        deployed = f"v{row['deployed_version']}" if row["deployed_version"] else "not deployed"
        print(f"{row['name']:<32} {row['customer']:<30} latest v{row['latest_version']}  ({deployed})")


def cmd_tools(args):
    spec, version = _store(args).load_spec(args.agent, args.version)
    print(f"{spec.name} v{version}")
    for t in spec.tools:
        gate = "approval" if t.requires_approval else "automatic"
        print(f"  {t.name:<28} {t.component:<18} {risk_of(t):<15} {gate}")
    print(f"  consequential: {'yes' if is_consequential(spec.tools) else 'no'}")


# ---------------------------------------------------------------- parser

def main(argv=None):
    parser = argparse.ArgumentParser(prog="agentgen", description="Generate, test, deploy and run AI agents.")
    parser.add_argument("--db", default=os.environ.get("AGENTGEN_DB", "agentgen.db"), help="Database file")
    sub = parser.add_subparsers(required=True)

    proj = sub.add_parser("project", help="Requirements-to-agent pipeline").add_subparsers(required=True)
    p = proj.add_parser("new", help="Create a project from an intake description and ask discovery questions")
    p.add_argument("intake")
    p.add_argument("--customer", required=True)
    p.add_argument("--title")
    p.set_defaults(func=cmd_project_new)
    p = proj.add_parser("answer", help="Answer the discovery questions")
    p.add_argument("id", type=int)
    p.add_argument("--file", help="YAML list of {question, answer}")
    p.set_defaults(func=cmd_project_answer)
    for name, func, text in [("analyze", cmd_project_analyze, "Analyse the process and recommend an agent"),
                             ("design", cmd_project_design, "Design the agent spec"),
                             ("show", cmd_project_show, "Show a project")]:
        p = proj.add_parser(name, help=text)
        p.add_argument("id", type=int)
        p.set_defaults(func=func)
    sub.add_parser("projects", help="List projects").set_defaults(func=cmd_projects)

    p = sub.add_parser("design", help="Shortcut: notes straight to an agent spec")
    p.add_argument("requirements")
    p.add_argument("--customer", required=True)
    p.add_argument("--analyze", action="store_true", help="Run the analysis stage first")
    p.set_defaults(func=cmd_design)

    p = sub.add_parser("export", help="Write an agent spec to YAML for review")
    p.add_argument("agent")
    p.add_argument("--version", type=int)
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("import", help="Save an edited YAML spec as a new version")
    p.add_argument("file")
    p.add_argument("--customer", required=True)
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("knowledge", help="Add documents to an agent's knowledge base")
    p.add_argument("agent")
    p.add_argument("files", nargs="+")
    p.set_defaults(func=cmd_knowledge)

    data = sub.add_parser("data", help="Customer datasets").add_subparsers(required=True)
    p = data.add_parser("import", help="Import a CSV file as a dataset")
    p.add_argument("customer")
    p.add_argument("name")
    p.add_argument("file")
    p.set_defaults(func=cmd_data_import)
    p = data.add_parser("list", help="List a customer's datasets")
    p.add_argument("customer")
    p.set_defaults(func=cmd_data_list)

    p = sub.add_parser("tools", help="Show an agent's tools, risk and approval policy")
    p.add_argument("agent")
    p.add_argument("--version", type=int)
    p.set_defaults(func=cmd_tools)

    p = sub.add_parser("test", help="Run the agent's test suite")
    p.add_argument("agent")
    p.add_argument("--version", type=int)
    p.add_argument("--threshold", type=float, default=0.9)
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_test)

    p = sub.add_parser("approve", help="Record a person's approval of a tested version")
    p.add_argument("agent")
    p.add_argument("--version", type=int)
    p.add_argument("--by", required=True)
    p.add_argument("--note")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("deploy", help="Deploy (or roll back to) a version that passed its tests")
    p.add_argument("agent")
    p.add_argument("--version", type=int)
    p.set_defaults(func=cmd_deploy)

    p = sub.add_parser("chat", help="Talk to the deployed agent")
    p.add_argument("agent")
    p.add_argument("message", nargs="?")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("trigger", help="Fire an event for a deployed agent")
    p.add_argument("agent")
    p.add_argument("event")
    p.add_argument("--payload", default="", help="Event data, or @file")
    p.add_argument("--run-now", action="store_true")
    p.set_defaults(func=cmd_trigger)

    p = sub.add_parser("worker", help="Run scheduled, event and follow-up jobs")
    p.add_argument("--once", action="store_true")
    p.add_argument("--poll", type=int, default=30)
    p.set_defaults(func=cmd_worker)

    acts = sub.add_parser("actions", help="Actions waiting for a person's approval").add_subparsers(required=True)
    acts.add_parser("list").set_defaults(func=cmd_actions_list)
    for name, approve in (("approve", True), ("deny", False)):
        p = acts.add_parser(name)
        p.add_argument("id", type=int)
        p.add_argument("--by", required=True)
        p.set_defaults(func=lambda a, approve=approve: cmd_actions_decide(a, approve))

    p = sub.add_parser("monitor", help="Operational metrics and cost")
    p.add_argument("agent")
    p.add_argument("--days", type=int, default=30)
    p.set_defaults(func=cmd_monitor)
    p = sub.add_parser("usage", help="Alias of monitor")
    p.add_argument("agent")
    p.add_argument("--days", type=int, default=30)
    p.set_defaults(func=cmd_monitor)

    sub.add_parser("list", help="List agents").set_defaults(func=cmd_list)
    sub.add_parser("components", help="List the capability library").set_defaults(
        func=lambda a: print(catalog_description()))

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (KeyError, ValueError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
