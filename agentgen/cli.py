"""Command-line interface.

Typical flow:
  agentgen design notes.md --customer "Andersson Redovisning AB"   # requirements -> spec v1
  agentgen export client-email-triage -o spec.yaml                 # review / edit by hand
  agentgen import spec.yaml --customer "Andersson Redovisning AB"  # saved as v2
  agentgen knowledge client-email-triage docs/*.md                 # load company knowledge
  agentgen test client-email-triage                                # run the test suite
  agentgen deploy client-email-triage                              # only if tests passed
  agentgen chat client-email-triage                                # use it (approvals prompt you)
"""

import argparse
import os
import sys
from pathlib import Path

from .components import catalog_description
from .llm import make_client
from .spec import AgentSpec
from .store import Store


def _store(args) -> Store:
    return Store(args.db)


def cmd_design(args):
    from .designer import design_agent

    store = _store(args)
    requirements = Path(args.requirements).read_text(encoding="utf-8")
    customer_id = store.get_or_create_customer(args.customer)
    req_id = store.add_requirements(customer_id, requirements)
    print("Designing agent from requirements...", file=sys.stderr)
    spec = design_agent(make_client(), requirements)
    version = store.save_version(customer_id, spec, req_id)
    print(spec.to_yaml())
    print(f"Saved {spec.name} v{version} (status: draft).", file=sys.stderr)
    if spec.open_questions:
        print("\nOpen questions to resolve with the customer:", file=sys.stderr)
        for q in spec.open_questions:
            print(f"  - {q}", file=sys.stderr)


def cmd_export(args):
    spec, version = _store(args).load_spec(args.agent, args.version)
    text = spec.to_yaml()
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"Wrote {args.agent} v{version} to {args.output}", file=sys.stderr)
    else:
        print(text)


def cmd_import(args):
    from .designer import validate_design

    store = _store(args)
    spec = validate_design(AgentSpec.from_yaml(Path(args.file).read_text(encoding="utf-8")))
    version = store.save_version(store.get_or_create_customer(args.customer), spec)
    print(f"Saved {spec.name} v{version} (status: draft).")


def cmd_knowledge(args):
    store = _store(args)
    agent_id = store.agent(args.agent)["id"]
    for f in args.files:
        path = Path(f)
        store.add_knowledge(agent_id, path.stem, path.read_text(encoding="utf-8"))
        print(f"Added {path.name}")


def cmd_test(args):
    from .evaluate import evaluate

    def show(case):
        mark = "PASS" if case.passed else "FAIL"
        print(f"[{mark}] {case.name}")
        if not case.passed:
            print(f"       tools: {case.tool_check}")
            print(f"       judge: {case.judge_reasoning}")
        if args.verbose:
            print("       " + case.transcript.replace("\n", "\n       "))

    report = evaluate(make_client(), _store(args), args.agent, args.version, args.threshold, on_case=show)
    verdict = "PASSED" if report.passed else "FAILED"
    print(f"\n{args.agent} v{report.version}: {report.pass_rate:.0%} of {len(report.cases)} cases "
          f"(threshold {report.threshold:.0%}) - {verdict}")
    sys.exit(0 if report.passed else 1)


def cmd_deploy(args):
    store = _store(args)
    version = args.version or store.load_spec(args.agent)[1]
    store.deploy(args.agent, version)
    print(f"{args.agent} now runs v{version}.")


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
    history: list = []
    if args.message:
        print(runtime.run(args.message).text)
        return
    print(f"Chatting with {args.agent} v{version}. Ctrl-D to quit.")
    while True:
        try:
            msg = input("\nyou> ")
        except EOFError:
            break
        result = runtime.run(msg, history)
        history = result.messages
        print(f"\n{args.agent}> {result.text}")


def cmd_list(args):
    for row in _store(args).list_agents():
        deployed = f"v{row['deployed_version']}" if row["deployed_version"] else "not deployed"
        print(f"{row['name']:<32} {row['customer']:<30} latest v{row['latest_version']}  ({deployed})")


def cmd_usage(args):
    row = _store(args).usage_summary(args.agent)
    print(f"runs: {row['runs']}  input tokens: {row['input_tokens']}  output tokens: {row['output_tokens']}  "
          f"escalations: {row['escalations']}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agentgen", description="Generate, test and run AI agents.")
    parser.add_argument("--db", default=os.environ.get("AGENTGEN_DB", "agentgen.db"), help="Database file")
    sub = parser.add_subparsers(required=True)

    p = sub.add_parser("design", help="Generate an agent spec from requirements notes")
    p.add_argument("requirements")
    p.add_argument("--customer", required=True)
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

    p = sub.add_parser("test", help="Run the agent's test suite")
    p.add_argument("agent")
    p.add_argument("--version", type=int)
    p.add_argument("--threshold", type=float, default=0.9)
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_test)

    p = sub.add_parser("deploy", help="Deploy (or roll back to) a version that passed its tests")
    p.add_argument("agent")
    p.add_argument("--version", type=int)
    p.set_defaults(func=cmd_deploy)

    p = sub.add_parser("chat", help="Talk to the deployed agent")
    p.add_argument("agent")
    p.add_argument("message", nargs="?")
    p.set_defaults(func=cmd_chat)

    sub.add_parser("list", help="List agents").set_defaults(func=cmd_list)
    sub.add_parser("components", help="List available components").set_defaults(
        func=lambda a: print(catalog_description()))

    p = sub.add_parser("usage", help="Usage and cost summary for an agent")
    p.add_argument("agent")
    p.set_defaults(func=cmd_usage)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (KeyError, ValueError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
