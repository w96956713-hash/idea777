"""The web application: a JSON API over the agent engine, plus the browser UI.

Every endpoint requires a signed-in user and only ever touches that user's organisation.
Long model work (questions, analysis, design, tests, runs) runs as background operations
that the browser polls. A built-in worker runs deployed agents' schedules, events and
follow-ups (the local deployment mechanism).
"""

import json
import logging
import os
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import datasets as datasets_mod
from ..components import ALWAYS_APPROVE, CATALOG, is_consequential, org_secret, risk_of, secret_env_name
from ..designer import validate_design
from ..evaluate import evaluate
from ..config import default_budget_usd as configured_budget, model_settings
from ..diagnose import (FixProposal, add_test_case, apply_fix, diagnose_case, diagnose_run, propose_fix,
                        remove_test_case)
from ..llm import DEFAULT_MODEL, PRICES, cost_usd, make_provider, model_configured
from ..pipeline import provider_label, suggest_tests
from ..providers import ModelError, ModelNotConfigured
from ..providers.metering import metered as metered_provider
from ..monitor import agent_metrics
from ..pipeline import INTAKE_FIELDS, Analysis, analyze, compose_intake, design, estimate_monthly_cost
from ..pipeline import generate_questions, record_answers
from ..runtime import AgentRuntime, approve_all, execute_queued_action, queue_all
from ..spec import SAFETY_CATEGORIES, AgentSpec, ConfigItem, TestCase
from ..store import Store, utcnow
from ..worker import fire_event, recover_interrupted_jobs, run_due_jobs, schedule_due
from . import auth

log = logging.getLogger("agentgen.web")
STATIC = Path(__file__).parent / "static"
DEFAULT_BUDGET_USD = None  # from AGENTGEN_DEFAULT_BUDGET_USD (default 25)
MAX_CSV_BYTES = 5_000_000
MAX_DOC_BYTES = 1_000_000


# ---------------------------------------------------------------- request bodies

class SignupIn(BaseModel):
    organization: str = Field(min_length=2, max_length=120)
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=200, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=auth.MIN_PASSWORD, max_length=200)


class LoginIn(BaseModel):
    email: str = Field(max_length=200)
    password: str = Field(max_length=200)


class MemberIn(SignupIn):
    organization: str = ""
    role: Literal["admin", "member"] = "member"


class IntakeIn(BaseModel):
    title: str = Field(min_length=2, max_length=160)
    company: str = Field(min_length=1, max_length=200)
    industry: str = Field(default="", max_length=200)
    problem: str = Field(min_length=20, max_length=6000)
    workflow: str = Field(default="", max_length=6000)
    outcome: str = Field(default="", max_length=4000)
    systems: str = Field(default="", max_length=4000)
    constraints: str = Field(default="", max_length=4000)


class Answer(BaseModel):
    question: str = Field(max_length=2000)
    answer: str = Field(default="", max_length=4000)


class AnswersIn(BaseModel):
    answers: list[Answer] = Field(max_length=40)


class AmendmentsIn(BaseModel):
    amendments: str = Field(default="", max_length=8000)


class ToolEdit(BaseModel):
    name: str
    requires_approval: bool
    config: dict[str, str] = Field(default_factory=dict)


class ConfigIn(BaseModel):
    tools: list[ToolEdit] | None = None
    max_turns: int | None = Field(default=None, ge=3, le=50)
    max_tokens_per_run: int | None = Field(default=None, ge=20000, le=1_000_000)
    model: str | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    instructions: str | None = Field(default=None, min_length=20, max_length=20000)


class YamlIn(BaseModel):
    yaml: str = Field(min_length=10, max_length=200_000)


class RunIn(BaseModel):
    task: str = Field(min_length=1, max_length=20000)
    mode: Literal["sandbox", "live"] = "sandbox"
    version: int | None = None


class EventIn(BaseModel):
    event: str = Field(min_length=1, max_length=100)
    payload: str = Field(default="", max_length=20000)


class FixtureIn(BaseModel):
    tool: str = Field(max_length=64)
    response: str = Field(max_length=20000)


class TestCaseIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    category: Literal["task", "quality", "tool_use", "missing_info", "permission", "unauthorized_action",
                      "api_failure", "edge_case"]
    input: str = Field(min_length=1, max_length=20000)
    expected_behavior: str = Field(min_length=1, max_length=4000)
    must_call: list[str] = Field(default_factory=list, max_length=20)
    must_not_call: list[str] = Field(default_factory=list, max_length=20)
    fail_tools: list[str] = Field(default_factory=list, max_length=20)
    fixtures: list[FixtureIn] = Field(default_factory=list, max_length=20)
    must_include: list[str] = Field(default_factory=list, max_length=20)
    must_not_include: list[str] = Field(default_factory=list, max_length=20)


class TestCasesIn(BaseModel):
    cases: list[TestCaseIn] = Field(min_length=1, max_length=20)


class FixRequestIn(BaseModel):
    run_id: int | None = None
    case_name: str | None = Field(default=None, max_length=120)
    note: str = Field(default="", max_length=4000)


class ApplyFixIn(BaseModel):
    proposal: FixProposal


class NoteIn(BaseModel):
    note: str = Field(default="", max_length=2000)


class DeployIn(BaseModel):
    version: int


class DecisionIn(BaseModel):
    decision: Literal["approve", "deny"]


class DatasetIn(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    csv: str = Field(min_length=1)


class DocumentIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)


class SettingsIn(BaseModel):
    monthly_budget_usd: float = Field(ge=0, le=100_000)


# ---------------------------------------------------------------- helpers

def friendly_error(e: Exception) -> str:
    """A message a business user can act on. Provider errors carry their own; nothing else leaks
    internals (full details go to the server log)."""
    if isinstance(e, ModelError):
        return e.user_message
    if isinstance(e, (ValueError, KeyError)):
        return str(e).strip("'\"")
    return "Something went wrong on the server. The details are in the server log."


def row(r) -> dict | None:
    return dict(r) if r is not None else None


def integration_status(tool, customer_id: int) -> dict:
    cfg = tool.config_dict()
    if tool.component == "http_request":
        secret = cfg.get("auth_env", "")
        configured = bool(org_secret(customer_id, secret)) if secret else None
        status = "Sandbox runs and tests use simulated responses; external calls happen only in live runs"
        if secret:
            status += (", with the organisation secret " + secret + (" (configured)." if configured else
                       f" (not configured: set {secret_env_name(customer_id, secret)} on the server)."))
        else:
            status += " (no authentication)."
        return {"kind": "external_api", "label": "External API", "host": urlparse(cfg.get("base_url", "")).netloc,
                "live_ready": bool(configured or not secret), "secret_name": secret or None,
                "secret_env": secret_env_name(customer_id, secret) if secret else None,
                "secret_configured": configured, "status": status}
    if tool.component == "send_message":
        return {"kind": "messaging", "label": f"Messages ({cfg.get('channel', 'email')})", "live_ready": False,
                "status": "Outbox only: approved messages are stored for delivery. No delivery connector is "
                          "installed, so nothing is actually sent."}
    if tool.component == "draft_message":
        return {"kind": "messaging", "label": "Drafts", "live_ready": True,
                "status": "Drafts are stored in the platform outbox for people to review."}
    return {"kind": "built_in", "label": "Built in", "live_ready": True,
            "status": "Runs inside the platform on your organisation's own data."}


def tool_info(tool, customer_id: int) -> dict:
    comp = CATALOG[tool.component]
    risk = risk_of(tool)
    return {
        "name": tool.name, "component": tool.component, "purpose": tool.purpose,
        "requires_approval": tool.requires_approval, "risk": risk,
        "approval_forced": risk in ALWAYS_APPROVE, "config": tool.config_dict(),
        "config_keys": comp.config_keys, "required_config": list(comp.required_config),
        "summary": comp.summary, "integration": integration_status(tool, customer_id),
    }


def report_summary(report: dict | None) -> dict | None:
    if not report:
        return None
    cases = report.get("cases", [])
    return {
        "pass_rate": report.get("pass_rate"), "passed": report.get("passed"), "threshold": report.get("threshold"),
        "blockers": report.get("blockers", []), "total": len(cases), "passed_count": sum(c["passed"] for c in cases),
        "safety_failures": report.get("safety_failures", []), "regressions": report.get("regressions", []),
        "avg_usd_per_run": report.get("avg_usd_per_run"),
    }


# ---------------------------------------------------------------- application

def create_app(db_path: str | None = None, provider_factory=None, default_budget_usd: float | None = DEFAULT_BUDGET_USD,
               start_worker: bool = True, worker_poll_seconds: int = 15, inline_operations: bool = False,
               secure_cookies: bool | None = None, allow_signup: bool | None = None) -> FastAPI:
    db_path = db_path or os.environ.get("AGENTGEN_DB", "agentgen.db")
    Store(db_path).close()  # create/migrate the schema once
    boot = Store(db_path, migrate=False)
    boot.fail_interrupted_operations()
    boot.close()

    if default_budget_usd is None:
        default_budget_usd = configured_budget()
    if provider_factory is None:
        provider_factory = make_provider  # raises ModelNotConfigured without ANTHROPIC_API_KEY
        model_ready = model_configured
        provider_meta = {"name": "anthropic", "simulated": False}
    else:
        model_ready = lambda: True  # noqa: E731 - injected provider (tests)
        probe = provider_factory()
        provider_meta = {"name": probe.name, "simulated": bool(probe.simulated)}

    if secure_cookies is None:
        secure_cookies = os.environ.get("AGENTGEN_SECURE_COOKIES", "") == "1"
    if allow_signup is None:
        allow_signup = os.environ.get("AGENTGEN_ALLOW_SIGNUP", "1") == "1"

    executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="agentgen-op")
    throttle = auth.LoginThrottle()
    stop_worker = threading.Event()
    worker_state = {"running": False, "last_tick": None, "last_error": None}

    @asynccontextmanager
    async def lifespan(_app):
        if start_worker:
            threading.Thread(target=worker_loop, name="agentgen-worker", daemon=True).start()
        yield
        stop_worker.set()
        executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="agentgen", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    def open_store() -> Store:
        return Store(db_path, migrate=False)

    def metered(customer_id: int, purpose: str):
        """The model provider for one organisation and purpose: budget-checked before every call,
        usage recorded after. Raises ModelNotConfigured without an API key."""
        return metered_provider(provider_factory(), open_store, customer_id, purpose, default_budget_usd)

    def provider_status() -> dict:
        s = model_settings()
        return {**s.public(), "provider": provider_meta["name"], "simulated": provider_meta["simulated"],
                "ready": model_ready(), "api_key_configured": s.api_key_present}

    # ------------------------------------------------------------ middleware & dependencies

    @app.middleware("http")
    async def security(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS"):
            # CSRF: browsers can't send this header cross-site without a CORS preflight we never allow.
            if request.headers.get("x-requested-with") != "agentgen":
                return JSONResponse({"detail": "Missing request header"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def get_store():
        st = open_store()
        try:
            yield st
        finally:
            st.close()

    def current_user(request: Request, st: Store = Depends(get_store)) -> dict:
        user = auth.session_user(st, request.cookies.get(auth.COOKIE))
        if user is None:
            raise HTTPException(401, "Please sign in")
        return dict(user)

    def admin_user(user: dict = Depends(current_user)) -> dict:
        if user["role"] != "admin":
            raise HTTPException(403, "Only administrators can do this")
        return user

    def scoped_agent(st: Store, user: dict, agent_id: int):
        agent = st.agent_scoped(user["customer_id"], agent_id)
        if agent is None:
            raise HTTPException(404, "Agent not found")
        return agent

    def scoped_project(st: Store, user: dict, project_id: int):
        try:
            project = st.project(project_id)
        except KeyError:
            project = None
        if project is None or project["customer_id"] != user["customer_id"]:
            raise HTTPException(404, "Project not found")
        return project

    def load_version(st: Store, agent, version: int | None):
        try:
            return st.load_spec(agent["name"], version)
        except KeyError:
            raise HTTPException(404, "Version not found")

    # ------------------------------------------------------------ background operations

    def start_operation(st: Store, user: dict, kind: str, target: dict, fn, needs_model: bool = True) -> dict:
        if needs_model and not model_ready():
            raise HTTPException(400, "No model API key is configured on the server. Set ANTHROPIC_API_KEY and "
                                     "restart to use this.")
        for op in st.operations_for(user["customer_id"], kind=kind):
            if op["status"] in ("queued", "running") and json.loads(op["target_json"]) == target:
                raise HTTPException(409, "This is already running")
        op_id = st.create_operation(user["customer_id"], kind, target, user["name"])

        def run():
            ost = open_store()
            try:
                ost.update_operation(op_id, "running")
                result = fn(ost, op_id)
                ost.update_operation(op_id, "succeeded", result)
            except Exception as e:  # reported to the user through the operation
                if isinstance(e, ModelError):
                    log.warning("operation %s (%s) failed: %s", op_id, kind, e)
                else:
                    log.warning("operation %s (%s) failed: %s", op_id, kind, traceback.format_exc())
                details = {**(getattr(e, "op_result", None) or {}), "error_kind": type(e).__name__,
                           "retryable": bool(getattr(e, "retryable", False))}
                ost.update_operation(op_id, "failed", details, friendly_error(e))
            finally:
                ost.close()

        if inline_operations:
            run()
        else:
            executor.submit(run)
        return op_payload(open_store_row(op_id))

    def open_store_row(op_id: int):
        st = open_store()
        try:
            return st.operation(op_id)
        finally:
            st.close()

    def op_payload(op) -> dict:
        return {"id": op["id"], "kind": op["kind"], "status": op["status"], "target": json.loads(op["target_json"]),
                "result": json.loads(op["result_json"]) if op["result_json"] else None, "error": op["error"],
                "created_at": op["created_at"], "updated_at": op["updated_at"], "created_by": op["created_by"]}

    @app.get("/api/operations/{op_id}")
    def get_operation(op_id: int, user=Depends(current_user), st: Store = Depends(get_store)):
        op = st.operation(op_id)
        if op is None or op["customer_id"] != user["customer_id"]:
            raise HTTPException(404, "Operation not found")
        return op_payload(op)

    # ------------------------------------------------------------ auth & account

    def set_session_cookie(response: Response, token: str):
        response.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True,
                            samesite="strict", secure=secure_cookies, path="/")

    @app.post("/api/auth/signup")
    def signup(body: SignupIn, response: Response, st: Store = Depends(get_store)):
        if not allow_signup:
            raise HTTPException(403, "Sign-up is disabled on this server")
        org = body.organization.strip()
        if st.customer_exists(org):
            raise HTTPException(409, "An organisation with this name already exists. Ask its administrator to add you.")
        if st.user_by_email(body.email):
            raise HTTPException(409, "An account with this email already exists")
        customer_id = st.get_or_create_customer(org)
        user_id = st.create_user(customer_id, body.email, body.name.strip(), auth.hash_password(body.password), "admin")
        set_session_cookie(response, auth.start_session(st, user_id))
        return {"ok": True}

    @app.post("/api/auth/login")
    def login(body: LoginIn, response: Response, st: Store = Depends(get_store)):
        if throttle.blocked(body.email):
            raise HTTPException(429, "Too many failed sign-in attempts. Try again in 15 minutes.")
        user = st.user_by_email(body.email)
        if user is None or not auth.verify_password(body.password, user["password_hash"]):
            throttle.fail(body.email)
            raise HTTPException(401, "Wrong email or password")
        throttle.clear(body.email)
        set_session_cookie(response, auth.start_session(st, user["id"]))
        return {"ok": True}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response, st: Store = Depends(get_store)):
        auth.end_session(st, request.cookies.get(auth.COOKIE))
        response.delete_cookie(auth.COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/me")
    def me(user=Depends(current_user)):
        return {"user": {k: user[k] for k in ("id", "name", "email", "role")}, "organization": user["organization"],
                "model_configured": model_ready(), "signup_enabled": allow_signup,
                "provider": {"name": provider_meta["name"], "simulated": provider_meta["simulated"]}}

    @app.get("/api/users")
    def list_users(user=Depends(admin_user), st: Store = Depends(get_store)):
        rows = st.conn.execute("SELECT id, name, email, role, created_at FROM users WHERE customer_id = ? ORDER BY id",
                               (user["customer_id"],)).fetchall()
        return [dict(r) for r in rows]

    @app.post("/api/users")
    def add_user(body: MemberIn, user=Depends(admin_user), st: Store = Depends(get_store)):
        if st.user_by_email(body.email):
            raise HTTPException(409, "An account with this email already exists")
        st.create_user(user["customer_id"], body.email, body.name.strip(), auth.hash_password(body.password), body.role)
        return {"ok": True}

    # ------------------------------------------------------------ settings & overview

    @app.get("/api/settings")
    def get_settings(user=Depends(current_user), st: Store = Depends(get_store)):
        month_start = utcnow().replace(day=1, hour=0, minute=0, second=0).isoformat()
        return {
            "model_configured": model_ready(), "default_model": model_settings().default_model,
            "model": provider_status(),
            "prices_usd_per_mtok": {m: {"input": p[0], "output": p[1]} for m, p in PRICES.items()},
            "monthly_budget_usd": st.monthly_budget(user["customer_id"], default_budget_usd),
            "month_spend_usd": round(st.month_spend(user["customer_id"], simulated=provider_meta["simulated"]), 4),
            "month_spend_real_usd": round(st.month_spend(user["customer_id"], simulated=False), 4),
            "month_spend_simulated_usd": round(st.month_spend(user["customer_id"], simulated=True), 4),
            "usage_by_purpose": [dict(r) for r in st.usage_by_purpose(user["customer_id"], month_start)],
            "worker": {"enabled": start_worker, "poll_seconds": worker_poll_seconds, **worker_state},
            "secret_prefix": secret_env_name(user["customer_id"], ""),
        }

    @app.post("/api/settings/check-model")
    def check_model(user=Depends(admin_user)):
        """Verify the API key and default model through the Models API. Uses no tokens."""
        model = model_settings().default_model
        try:
            provider = provider_factory()
        except ModelError as e:
            return {"ok": False, "provider": provider_meta["name"], "model": model, "message": e.user_message}
        status = provider.check(model)
        if provider_meta["simulated"]:
            status.message = "Test mode: a scripted test model is answering instead of a real model."
        return {"ok": status.ok, "provider": status.provider, "model": status.model, "message": status.message,
                "simulated": provider_meta["simulated"]}

    @app.put("/api/settings")
    def put_settings(body: SettingsIn, user=Depends(admin_user), st: Store = Depends(get_store)):
        st.set_monthly_budget(user["customer_id"], body.monthly_budget_usd)
        return {"ok": True}

    @app.get("/api/overview")
    def overview(user=Depends(current_user), st: Store = Depends(get_store)):
        cid = user["customer_id"]
        agents = st.agents_for(cid)
        week_ago = (utcnow() - timedelta(days=7)).isoformat()
        runs = st.runs_for(cid, limit=500)
        recent = [r for r in runs if r["created_at"] >= week_ago]
        return {
            "model_configured": model_ready(),
            "projects": len(st.list_projects_for(cid)),
            "agents": len(agents),
            "deployed": sum(1 for a in agents if a["deployed_version"] is not None),
            "runs_7d": len(recent),
            "failed_runs_7d": sum(1 for r in recent if r["stop_reason"] not in ("end_turn", None)),
            "pending_approvals": len(st.actions_for(cid)),
            "month_spend_usd": round(st.month_spend(cid, simulated=provider_meta["simulated"]), 4),
            "monthly_budget_usd": st.monthly_budget(cid, default_budget_usd),
            "provider": {"name": provider_meta["name"], "simulated": provider_meta["simulated"]},
            "recent_runs": [run_row(st, r, {}) for r in runs[:8]],
            "running_operations": [op_payload(o) for o in st.operations_for(cid) if o["status"] in ("queued", "running")],
            "agents_needing_attention": [
                {"id": a["id"], "name": a["name"], "status": a["latest_status"]}
                for a in agents if a["latest_status"] == "failed"],
        }

    # ------------------------------------------------------------ projects & requirements pipeline

    def project_payload(st: Store, project) -> dict:
        analysis = json.loads(project["analysis_json"]) if project["analysis_json"] else None
        estimate = estimate_monthly_cost(Analysis(**analysis)) if analysis else None
        agent = None
        if project["agent_name"]:
            a = st.conn.execute("SELECT id, name FROM agents WHERE name = ? AND customer_id = ?",
                                (project["agent_name"], project["customer_id"])).fetchone()
            agent = row(a)
        ops = st.operations_for(project["customer_id"], target_key="project_id", target_value=project["id"], limit=10)
        return {
            "id": project["id"], "title": project["title"], "stage": project["stage"],
            "intake": json.loads(project["intake_json"]) if project["intake_json"] else {"problem": project["intake"]},
            "intake_text": project["intake"],
            "questions": json.loads(project["questions_json"])["questions"] if project["questions_json"] else [],
            "missing_information": (json.loads(project["questions_json"]).get("missing_information", [])
                                    if project["questions_json"] else []),
            "contradictions": (json.loads(project["questions_json"]).get("contradictions", [])
                               if project["questions_json"] else []),
            "generated_by": {"questions": project["questions_provider"], "analysis": project["analysis_provider"],
                             "design": project["design_provider"]},
            "answers": json.loads(project["answers_json"]) if project["answers_json"] else [],
            "analysis": analysis, "cost_estimate": estimate, "amendments": project["amendments"] or "",
            "agent": agent, "operations": [op_payload(o) for o in ops],
            "created_at": project["created_at"], "updated_at": project["updated_at"],
        }

    @app.get("/api/projects")
    def list_projects(user=Depends(current_user), st: Store = Depends(get_store)):
        return [dict(p) for p in st.list_projects_for(user["customer_id"])]

    @app.post("/api/projects")
    def create_project(body: IntakeIn, user=Depends(current_user), st: Store = Depends(get_store)):
        fields = body.model_dump()
        project_id = st.create_project(user["customer_id"], body.title.strip(), compose_intake(fields), fields)
        return project_payload(st, st.project(project_id))

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: int, user=Depends(current_user), st: Store = Depends(get_store)):
        return project_payload(st, scoped_project(st, user, project_id))

    @app.put("/api/projects/{project_id}/intake")
    def update_intake(project_id: int, body: IntakeIn, user=Depends(current_user), st: Store = Depends(get_store)):
        scoped_project(st, user, project_id)
        fields = body.model_dump()
        st.update_project(project_id, title=body.title.strip(), intake=compose_intake(fields),
                          intake_json=json.dumps(fields, ensure_ascii=False))
        return project_payload(st, st.project(project_id))

    @app.post("/api/projects/{project_id}/questions")
    def start_questions(project_id: int, user=Depends(current_user), st: Store = Depends(get_store)):
        scoped_project(st, user, project_id)
        cid = user["customer_id"]

        def work(ost, op_id):
            discovery = generate_questions(metered(cid, "requirements"), ost, project_id)
            return {"questions": len(discovery.questions)}

        return start_operation(st, user, "questions", {"project_id": project_id}, work)

    @app.put("/api/projects/{project_id}/answers")
    def save_answers(project_id: int, body: AnswersIn, user=Depends(current_user), st: Store = Depends(get_store)):
        scoped_project(st, user, project_id)
        record_answers(st, project_id, [a.model_dump() for a in body.answers])
        return project_payload(st, st.project(project_id))

    @app.post("/api/projects/{project_id}/analysis")
    def start_analysis(project_id: int, user=Depends(current_user), st: Store = Depends(get_store)):
        scoped_project(st, user, project_id)
        cid = user["customer_id"]

        def work(ost, op_id):
            analysis = analyze(metered(cid, "requirements"), ost, project_id)
            return {"complexity": analysis.complexity, "opportunities": len(analysis.opportunities)}

        return start_operation(st, user, "analysis", {"project_id": project_id}, work)

    @app.put("/api/projects/{project_id}/amendments")
    def save_amendments(project_id: int, body: AmendmentsIn, user=Depends(current_user),
                        st: Store = Depends(get_store)):
        scoped_project(st, user, project_id)
        st.update_project(project_id, amendments=body.amendments.strip())
        return project_payload(st, st.project(project_id))

    @app.post("/api/projects/{project_id}/design")
    def start_design(project_id: int, user=Depends(current_user), st: Store = Depends(get_store)):
        scoped_project(st, user, project_id)
        cid = user["customer_id"]

        def work(ost, op_id):
            spec, version = design(metered(cid, "requirements"), ost, project_id)
            agent = ost.agent(spec.name)
            return {"agent_id": agent["id"], "name": spec.name, "version": version,
                    "notes": spec.open_questions}

        return start_operation(st, user, "design", {"project_id": project_id}, work)

    # ------------------------------------------------------------ agents

    @app.get("/api/agents")
    def list_agents(user=Depends(current_user), st: Store = Depends(get_store)):
        out = []
        for a in st.agents_for(user["customer_id"]):
            spec, _ = st.load_spec(a["name"])
            report = st.latest_test_report(a["name"], a["latest_version"])
            out.append({"id": a["id"], "name": a["name"], "purpose": spec.purpose,
                        "latest_version": a["latest_version"], "latest_status": a["latest_status"],
                        "deployed_version": a["deployed_version"], "last_run_at": a["last_run_at"],
                        "consequential": is_consequential(spec.tools), "tools": len(spec.tools),
                        "triggers": [t.kind for t in spec.triggers], "latest_test": report_summary(report)})
        return out

    def simulated_results_block(report: dict | None) -> str | None:
        """Test results produced by the scripted test model can't qualify a version on a server that
        uses a real model."""
        if report and report.get("simulated") and not provider_meta["simulated"]:
            return ("These test results came from the scripted test model, not a real model. "
                    "Run the tests again before approving or deploying.")
        return None

    def version_payload(st: Store, user: dict, agent, spec: AgentSpec, version: int) -> dict:
        reports = st.test_reports(agent["name"], version, limit=1)
        latest = json.loads(reports[0]["report_json"]) if reports else None
        approval = st.approval_for(agent["name"], version)
        status = st.version_status(agent["name"], version)
        consequential = is_consequential(spec.tools)
        blocked_simulated = simulated_results_block(latest)
        vrow = st.conn.execute("SELECT origin, provider, note FROM agent_versions WHERE agent_id = ? AND version = ?",
                               (agent["id"], version)).fetchone()
        return {
            "agent": {"id": agent["id"], "name": agent["name"], "deployed_version": agent["deployed_version"]},
            "version": version, "status": status, "spec": spec.model_dump(),
            "tools": [tool_info(t, user["customer_id"]) for t in spec.tools if t.component in CATALOG],
            "consequential": consequential,
            "approval": row(approval),
            "latest_test": latest, "latest_test_summary": report_summary(latest),
            "latest_test_at": reports[0]["created_at"] if reports else None,
            "can_approve": status == "passed" and approval is None and consequential and not blocked_simulated,
            "can_deploy": status == "passed" and (approval is not None or not consequential)
                          and agent["deployed_version"] != version and not blocked_simulated,
            "simulated_results_block": blocked_simulated,
            "origin": vrow["origin"] if vrow else None, "generated_by": vrow["provider"] if vrow else None,
            "version_note": vrow["note"] if vrow else None,
            "safety_categories": sorted(SAFETY_CATEGORIES),
            "models": list(PRICES),
        }

    @app.get("/api/agents/{agent_id}")
    def get_agent(agent_id: int, user=Depends(current_user), st: Store = Depends(get_store)):
        agent = scoped_agent(st, user, agent_id)
        spec, version = st.load_spec(agent["name"])
        project = st.conn.execute("SELECT id, title FROM projects WHERE agent_name = ? AND customer_id = ?",
                                  (agent["name"], user["customer_id"])).fetchone()
        return {**version_payload(st, user, agent, spec, version),
                "versions": [dict(v) for v in st.versions(agent["id"])],
                "project": row(project),
                "pending_actions": len(st.pending_actions(agent["id"])),
                "jobs": [dict(j) for j in st.conn.execute(
                    "SELECT * FROM jobs WHERE agent_id = ? ORDER BY id DESC LIMIT 10", (agent["id"],))]}

    @app.get("/api/agents/{agent_id}/versions/{version}")
    def get_version(agent_id: int, version: int, user=Depends(current_user), st: Store = Depends(get_store)):
        agent = scoped_agent(st, user, agent_id)
        spec, version = load_version(st, agent, version)
        return version_payload(st, user, agent, spec, version)

    @app.get("/api/agents/{agent_id}/versions/{version}/yaml")
    def get_yaml(agent_id: int, version: int, user=Depends(current_user), st: Store = Depends(get_store)):
        agent = scoped_agent(st, user, agent_id)
        spec, _ = load_version(st, agent, version)
        return {"yaml": spec.to_yaml()}

    def save_new_version(st: Store, user: dict, agent, spec: AgentSpec, origin: str = "edited",
                         provider: str | None = None, note: str | None = None) -> dict:
        spec.name = agent["name"]
        spec = validate_design(spec)
        version = st.save_version(user["customer_id"], spec, origin=origin, provider=provider,
                                  note=note or f"{origin.replace('_', ' ').capitalize()} by {user['name']}")
        st.audit(agent["id"], None, "version_created", {"version": version, "by": user["name"], "origin": origin})
        return {"version": version, "notes": spec.open_questions}

    @app.put("/api/agents/{agent_id}/versions/{version}/config")
    def edit_config(agent_id: int, version: int, body: ConfigIn, user=Depends(current_user),
                    st: Store = Depends(get_store)):
        agent = scoped_agent(st, user, agent_id)
        spec, _ = load_version(st, agent, version)
        spec = spec.model_copy(deep=True)
        if body.tools is not None:
            edits = {t.name: t for t in body.tools}
            for tool in spec.tools:
                edit = edits.get(tool.name)
                if edit is None:
                    continue
                comp = CATALOG.get(tool.component)
                unknown = set(edit.config) - set(comp.config_keys if comp else {})
                if unknown:
                    raise HTTPException(422, f"Unknown settings for {tool.name}: {', '.join(sorted(unknown))}")
                tool.requires_approval = edit.requires_approval
                tool.config = [ConfigItem(key=k, value=v) for k, v in edit.config.items() if v != ""]
        if body.max_turns is not None:
            spec.limits.max_turns = body.max_turns
        if body.max_tokens_per_run is not None:
            spec.limits.max_tokens_per_run = body.max_tokens_per_run
        if body.model is not None:
            if body.model not in PRICES:
                raise HTTPException(422, "Unknown model")
            spec.model = body.model
        if body.effort is not None:
            spec.effort = body.effort
        if body.instructions is not None:
            spec.instructions = body.instructions
        return save_new_version(st, user, agent, spec, origin="edited", note=f"Configuration edited from v{version}")

    @app.put("/api/agents/{agent_id}/spec")
    def replace_spec(agent_id: int, body: YamlIn, user=Depends(current_user), st: Store = Depends(get_store)):
        agent = scoped_agent(st, user, agent_id)
        try:
            spec = AgentSpec.from_yaml(body.yaml)
        except Exception as e:
            raise HTTPException(422, f"The spec is not valid: {str(e)[:1500]}")
        return save_new_version(st, user, agent, spec, origin="imported")

    @app.post("/api/agents/{agent_id}/versions/{version}/test")
    def start_tests(agent_id: int, version: int, user=Depends(current_user), st: Store = Depends(get_store)):
        agent = scoped_agent(st, user, agent_id)
        spec, version = load_version(st, agent, version)
        cid, name = user["customer_id"], agent["name"]

        def work(ost, op_id):
            done = []

            def progress(case):
                done.append({"name": case.name, "passed": case.passed})
                ost.update_operation(op_id, "running", {"done": len(done), "cases": done})

            report = evaluate(metered(cid, "test"), ost, name, version, on_case=progress)
            return {"passed": report.passed, "pass_rate": report.pass_rate, "blockers": report.blockers(),
                    "version": version}

        return start_operation(st, user, "test", {"agent_id": agent_id, "version": version}, work)

    @app.get("/api/agents/{agent_id}/versions/{version}/tests")
    def list_tests(agent_id: int, version: int, user=Depends(current_user), st: Store = Depends(get_store)):
        agent = scoped_agent(st, user, agent_id)
        load_version(st, agent, version)
        out = []
        for r in st.test_reports(agent["name"], version):
            report = json.loads(r["report_json"])
            for case in report.get("cases", []):
                if not case.get("passed"):
                    case["diagnosis"] = [f.model_dump() for f in diagnose_case(case)]
            out.append({"id": r["id"], "created_at": r["created_at"], "report": report})
        return out

    # ---------------------------------------------------- test cases, diagnosis, fixes, restore

    def to_case(body: TestCaseIn) -> TestCase:
        return TestCase(**{**body.model_dump(), "fixtures": [f.model_dump() for f in body.fixtures]})

    @app.post("/api/agents/{agent_id}/versions/{version}/test-cases")
    def add_cases(agent_id: int, version: int, body: TestCasesIn, user=Depends(current_user),
                  st: Store = Depends(get_store)):
        agent = scoped_agent(st, user, agent_id)
        spec, _ = load_version(st, agent, version)
        existing = {c.name for c in spec.test_cases}
        for case in body.cases:
            if case.name in existing:
                raise HTTPException(409, f"A test case named {case.name!r} already exists")
            existing.add(case.name)
        try:
            for case in body.cases:
                spec = add_test_case(spec, to_case(case))
        except ValueError as e:
            raise HTTPException(422, str(e))
        names = ", ".join(c.name for c in body.cases)
        return save_new_version(st, user, agent, spec, origin="tests_added",
                                note=f"Added test case(s) {names} to v{version}")

    @app.delete("/api/agents/{agent_id}/versions/{version}/test-cases/{name}")
    def delete_case(agent_id: int, version: int, name: str, user=Depends(current_user),
                    st: Store = Depends(get_store)):
        agent = scoped_agent(st, user, agent_id)
        spec, _ = load_version(st, agent, version)
        try:
            spec = remove_test_case(spec, name)
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\""))
        return save_new_version(st, user, agent, spec, origin="tests_removed",
                                note=f"Removed test case {name} from v{version}")

    @app.post("/api/agents/{agent_id}/versions/{version}/suggest-tests")
    def start_suggest_tests(agent_id: int, version: int, user=Depends(current_user), st: Store = Depends(get_store)):
        agent = scoped_agent(st, user, agent_id)
        spec, version = load_version(st, agent, version)
        cid = user["customer_id"]

        def work(ost, op_id):
            provider = metered(cid, "requirements")
            cases = suggest_tests(provider, spec)
            return {"cases": [c.model_dump() for c in cases], "generated_by": provider_label(provider)}

        return start_operation(st, user, "suggest_tests", {"agent_id": agent_id, "version": version}, work)

    @app.get("/api/runs/{run_id}/diagnosis")
    def run_diagnosis(run_id: int, user=Depends(current_user), st: Store = Depends(get_store)):
        if st.run_scoped(user["customer_id"], run_id) is None:
            raise HTTPException(404, "Run not found")
        return [f.model_dump() for f in diagnose_run(st, run_id)]

    def failure_evidence(st: Store, user: dict, agent, version: int, body: FixRequestIn) -> str:
        if body.run_id is not None:
            r = st.run_scoped(user["customer_id"], body.run_id)
            if r is None or r["agent_id"] != agent["id"]:
                raise HTTPException(404, "Run not found")
            steps = []
            for e in st.run_events(body.run_id):
                if e["event"] in ("tool_call", "tool_error", "tool_blocked", "approval", "run_failed"):
                    steps.append(f"{e['event']}: {e['data_json'][:1500]}")
            findings = "\n".join(f"- {f.title}: {f.detail}" for f in diagnose_run(st, body.run_id))
            return (f"Run #{r['id']} (version {r['version']}), outcome {r['stop_reason']}, error: {r['error'] or 'none'}\n"
                    f"Task:\n{r['input']}\n\nSteps:\n" + "\n".join(steps)
                    + f"\n\nFinal answer:\n{r['output'] or ''}\n\nPlatform diagnosis:\n{findings}"
                    + (f"\n\nWhat the customer says is wrong:\n{body.note}" if body.note else ""))
        if body.case_name:
            report = st.latest_test_report(agent["name"], version) or {}
            case = next((c for c in report.get("cases", []) if c["name"] == body.case_name), None)
            spec, _ = st.load_spec(agent["name"], version)
            definition = next((c for c in spec.test_cases if c.name == body.case_name), None)
            if case is None:
                raise HTTPException(404, "Test case result not found")
            return (f"Failed test case {case['name']} ({case['category']}).\n"
                    f"Expected behaviour: {definition.expected_behavior if definition else '?'}\n"
                    f"Checks: {case['tool_check']}\nGrader: {case['judge_reasoning']}\nError: {case.get('error') or 'none'}\n"
                    f"Transcript:\n{case['transcript']}"
                    + (f"\n\nWhat the customer says is wrong:\n{body.note}" if body.note else ""))
        if body.note:
            return f"What the customer says is wrong:\n{body.note}"
        raise HTTPException(422, "Give a run, a failed test case or a description of the problem")

    @app.post("/api/agents/{agent_id}/versions/{version}/propose-fix")
    def start_propose_fix(agent_id: int, version: int, body: FixRequestIn, user=Depends(current_user),
                          st: Store = Depends(get_store)):
        agent = scoped_agent(st, user, agent_id)
        spec, version = load_version(st, agent, version)
        evidence = failure_evidence(st, user, agent, version, body)
        cid = user["customer_id"]

        def work(ost, op_id):
            provider = metered(cid, "fix")
            proposal = propose_fix(provider, spec, evidence)
            return {"proposal": proposal.model_dump(), "generated_by": provider_label(provider), "version": version}

        target = {"agent_id": agent_id, "version": version, "run_id": body.run_id, "case": body.case_name}
        return start_operation(st, user, "propose_fix", target, work)

    @app.post("/api/agents/{agent_id}/versions/{version}/apply-fix")
    def apply_fix_endpoint(agent_id: int, version: int, body: ApplyFixIn, user=Depends(current_user),
                           st: Store = Depends(get_store)):
        agent = scoped_agent(st, user, agent_id)
        spec, version = load_version(st, agent, version)
        new = apply_fix(spec, body.proposal)
        return save_new_version(st, user, agent, new, origin="fixed",
                                note=f"Fix applied to v{version}: {body.proposal.diagnosis[:300]}")

    @app.post("/api/agents/{agent_id}/versions/{version}/restore")
    def restore_version(agent_id: int, version: int, user=Depends(current_user), st: Store = Depends(get_store)):
        """Copy an earlier version as a new draft (it must pass its tests again before release)."""
        agent = scoped_agent(st, user, agent_id)
        spec, version = load_version(st, agent, version)
        return save_new_version(st, user, agent, spec.model_copy(deep=True), origin="restored",
                                note=f"Restored from v{version} by {user['name']}")

    @app.post("/api/agents/{agent_id}/versions/{version}/approve")
    def approve_version(agent_id: int, version: int, body: NoteIn, user=Depends(admin_user),
                        st: Store = Depends(get_store)):
        agent = scoped_agent(st, user, agent_id)
        load_version(st, agent, version)
        report = st.latest_test_report(agent["name"], version)
        if simulated_results_block(report):
            raise HTTPException(400, simulated_results_block(report))
        if not report or not report.get("passed") or not report.get("has_safety_cases"):
            raise HTTPException(400, "Only versions whose latest test run passed, including all safety and "
                                     "permission tests, can be approved")
        try:
            st.approve_version(agent["name"], version, user["name"], body.note)
        except ValueError as e:
            raise HTTPException(400, str(e))
        st.audit(agent["id"], None, "version_approved", {"version": version, "by": user["name"], "note": body.note})
        return {"ok": True}

    @app.post("/api/agents/{agent_id}/deploy")
    def deploy(agent_id: int, body: DeployIn, user=Depends(admin_user), st: Store = Depends(get_store)):
        agent = scoped_agent(st, user, agent_id)
        spec, version = load_version(st, agent, body.version)
        blocked = simulated_results_block(st.latest_test_report(agent["name"], version))
        if blocked:
            raise HTTPException(400, blocked)
        try:
            st.deploy(agent["name"], version, requires_approval=is_consequential(spec.tools))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "deployed_version": version}

    @app.post("/api/agents/{agent_id}/undeploy")
    def undeploy(agent_id: int, user=Depends(admin_user), st: Store = Depends(get_store)):
        agent = scoped_agent(st, user, agent_id)
        if agent["deployed_version"] is None:
            raise HTTPException(400, "The agent is not deployed")
        st.undeploy(agent["name"])
        return {"ok": True}

    @app.post("/api/agents/{agent_id}/runs")
    def start_run(agent_id: int, body: RunIn, user=Depends(current_user), st: Store = Depends(get_store)):
        agent = scoped_agent(st, user, agent_id)
        cid = user["customer_id"]
        if body.mode == "live":
            if agent["deployed_version"] is None:
                raise HTTPException(400, "Live runs need a deployed version. Use a sandbox run to try a version.")
            spec, version = st.load_spec(agent["name"], agent["deployed_version"])
        else:
            spec, version = load_version(st, agent, body.version)
        name = agent["name"]

        def work(ost, op_id):
            client = metered(cid, f"run:{body.mode}")  # a ModelProvider
            real_agent = ost.agent(name)
            if body.mode == "sandbox":
                data_store, data_agent_id = ost.sandbox_for(name)
                runtime = AgentRuntime(client, data_store, data_agent_id, spec, version, approver=approve_all,
                                       trigger_source="sandbox", sandbox=True, log_store=ost,
                                       log_agent_id=real_agent["id"])
            else:
                runtime = AgentRuntime(client, ost, real_agent["id"], spec, version, approver=queue_all,
                                       trigger_source="manual")
            try:
                result = runtime.run(body.task)
            except Exception as e:
                run = ost.conn.execute("SELECT id FROM runs WHERE agent_id = ? ORDER BY id DESC LIMIT 1",
                                       (real_agent["id"],)).fetchone()
                e.op_result = {"run_id": run["id"] if run else None}
                raise
            return {"run_id": result.run_id, "stop_reason": result.stop_reason, "text": result.text,
                    "mode": body.mode, "version": version}

        return start_operation(st, user, "run", {"agent_id": agent_id, "mode": body.mode, "task": body.task[:200]},
                               work)

    @app.post("/api/agents/{agent_id}/events")
    def post_event(agent_id: int, body: EventIn, user=Depends(current_user), st: Store = Depends(get_store)):
        agent = scoped_agent(st, user, agent_id)
        try:
            jobs = fire_event(st, agent["name"], body.event, body.payload)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"jobs": jobs}

    @app.get("/api/agents/{agent_id}/metrics")
    def metrics(agent_id: int, days: int = 30, user=Depends(current_user), st: Store = Depends(get_store)):
        agent = scoped_agent(st, user, agent_id)
        return agent_metrics(st, agent["name"], max(1, min(days, 365)))

    # ------------------------------------------------------------ runs, approvals, jobs, monitoring

    def run_row(st: Store, r, models: dict) -> dict:
        key = (r["agent"], r["version"])
        if key not in models:
            try:
                models[key] = st.load_spec(r["agent"], r["version"])[0].model
            except KeyError:
                models[key] = DEFAULT_MODEL
        model = models[key]
        return {"id": r["id"], "agent_id": r["agent_id"], "agent": r["agent"], "version": r["version"],
                "source": r["trigger_source"], "stop_reason": r["stop_reason"],
                "status": "running" if r["stop_reason"] is None else
                          ("completed" if r["stop_reason"] == "end_turn" else "failed"),
                "input": r["input"][:300], "duration_ms": r["duration_ms"], "input_tokens": r["input_tokens"],
                "output_tokens": r["output_tokens"], "cost_usd": cost_usd(model, r["input_tokens"], r["output_tokens"]),
                "error": r["error"], "created_at": r["created_at"], "provider": r["provider"],
                "simulated": bool(r["simulated"])}

    @app.get("/api/runs")
    def list_runs(agent_id: int | None = None, source: str | None = None, status: str | None = None,
                  user=Depends(current_user), st: Store = Depends(get_store)):
        if agent_id is not None:
            scoped_agent(st, user, agent_id)
        models: dict = {}
        rows = [run_row(st, r, models) for r in st.runs_for(user["customer_id"], agent_id, limit=300)]
        if source:
            rows = [r for r in rows if r["source"] == source or (source == "live" and r["source"] != "sandbox")]
        if status:
            rows = [r for r in rows if r["status"] == status]
        return rows

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: int, user=Depends(current_user), st: Store = Depends(get_store)):
        r = st.run_scoped(user["customer_id"], run_id)
        if r is None:
            raise HTTPException(404, "Run not found")
        events = []
        for e in st.run_events(run_id):
            events.append({"event": e["event"], "data": json.loads(e["data_json"]), "created_at": e["created_at"]})
        actions = [dict(a) for a in st.conn.execute(
            """SELECT p.*, a.name AS agent FROM pending_actions p JOIN agents a ON a.id = p.agent_id
               WHERE p.run_id = ? ORDER BY p.id""", (run_id,))]
        return {**run_row(st, r, {}), "input": r["input"], "output": r["output"], "events": events,
                "actions": actions, "sandbox": r["trigger_source"] == "sandbox"}

    @app.get("/api/approvals")
    def approvals(user=Depends(current_user), st: Store = Depends(get_store)):
        cid = user["customer_id"]
        awaiting = []
        for a in st.agents_for(cid):
            for v in st.versions(a["id"]):
                if v["status"] == "passed" and v["approved_by"] is None:
                    spec, _ = st.load_spec(a["name"], v["version"])
                    if is_consequential(spec.tools):
                        awaiting.append({"agent_id": a["id"], "agent": a["name"], "version": v["version"],
                                         "pass_rate": v["pass_rate"], "created_at": v["created_at"]})
        return {"actions": [dict(a) for a in st.actions_for(cid, "pending")],
                "decided": [dict(a) for a in st.actions_for(cid, None) if a["status"] != "pending"][:30],
                "versions": awaiting}

    @app.post("/api/approvals/actions/{action_id}")
    def decide_action(action_id: int, body: DecisionIn, user=Depends(admin_user), st: Store = Depends(get_store)):
        action = st.conn.execute(
            """SELECT p.* FROM pending_actions p JOIN agents a ON a.id = p.agent_id
               WHERE p.id = ? AND a.customer_id = ?""", (action_id, user["customer_id"])).fetchone()
        if action is None:
            raise HTTPException(404, "Action not found")
        try:
            result = execute_queued_action(st, action_id, user["name"], body.decision == "approve")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"result": result}

    @app.get("/api/jobs")
    def jobs(user=Depends(current_user), st: Store = Depends(get_store)):
        return [dict(j) for j in st.jobs_for(user["customer_id"])]

    @app.get("/api/monitoring")
    def monitoring(days: int = 30, user=Depends(current_user), st: Store = Depends(get_store)):
        cid = user["customer_id"]
        days = max(1, min(days, 365))
        since = (utcnow() - timedelta(days=days)).isoformat()
        return {
            "days": days,
            "agents": [agent_metrics(st, a["name"], days) | {"id": a["id"]} for a in st.agents_for(cid)],
            "usage": [dict(r) for r in st.usage_by_purpose(cid, since)],
            "failed_jobs": [dict(j) for j in st.jobs_for(cid) if j["status"] == "failed"][:20],
            "month_spend_usd": round(st.month_spend(cid, simulated=provider_meta["simulated"]), 4),
            "monthly_budget_usd": st.monthly_budget(cid, default_budget_usd),
            "simulated": provider_meta["simulated"],
        }

    # ------------------------------------------------------------ data & integrations

    @app.get("/api/data")
    def data(user=Depends(current_user), st: Store = Depends(get_store)):
        cid = user["customer_id"]
        return {"datasets": [{"name": d["name"], "columns": json.loads(d["columns_json"]), "rows": d["row_count"]}
                             for d in st.datasets(cid)],
                "documents": [dict(d) for d in st.documents(cid)]}

    @app.post("/api/datasets")
    def upload_dataset(body: DatasetIn, user=Depends(current_user), st: Store = Depends(get_store)):
        if len(body.csv.encode()) > MAX_CSV_BYTES:
            raise HTTPException(413, "CSV files are limited to 5 MB")
        try:
            columns, rows = datasets_mod.import_csv_text(st, user["customer_id"], body.name, body.csv)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"name": body.name, "columns": columns, "rows": rows}

    @app.get("/api/datasets/{name}")
    def dataset_preview(name: str, user=Depends(current_user), st: Store = Depends(get_store)):
        try:
            return datasets_mod.preview(st, user["customer_id"], name)
        except KeyError:
            raise HTTPException(404, "Dataset not found")

    @app.delete("/api/datasets/{name}")
    def delete_dataset(name: str, user=Depends(admin_user), st: Store = Depends(get_store)):
        try:
            datasets_mod.delete(st, user["customer_id"], name)
        except KeyError:
            raise HTTPException(404, "Dataset not found")
        return {"ok": True}

    @app.post("/api/documents")
    def upload_document(body: DocumentIn, user=Depends(current_user), st: Store = Depends(get_store)):
        if len(body.content.encode()) > MAX_DOC_BYTES:
            raise HTTPException(413, "Documents are limited to 1 MB of text")
        return {"id": st.add_document(user["customer_id"], body.title.strip(), body.content)}

    @app.delete("/api/documents/{doc_id}")
    def delete_document(doc_id: int, user=Depends(admin_user), st: Store = Depends(get_store)):
        if not st.delete_document(user["customer_id"], doc_id):
            raise HTTPException(404, "Document not found")
        return {"ok": True}

    @app.get("/api/integrations")
    def integrations(user=Depends(current_user), st: Store = Depends(get_store)):
        out = []
        for a in st.agents_for(user["customer_id"]):
            spec, version = st.load_spec(a["name"])
            for t in spec.tools:
                if t.component in CATALOG:
                    out.append({"agent_id": a["id"], "agent": a["name"], "version": version, "tool": t.name,
                                "component": t.component, "risk": risk_of(t),
                                **integration_status(t, user["customer_id"])})
        return {"model": {"configured": model_ready(), "provider": "Anthropic Claude API",
                          "default_model": DEFAULT_MODEL},
                "tools": out}

    # ------------------------------------------------------------ UI

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    # ------------------------------------------------------------ local deployment worker

    def worker_loop():
        st = open_store()
        worker_state["running"] = True
        try:
            try:
                recover_interrupted_jobs(st)
            except Exception:  # never stop the worker over this
                log.exception("recovering interrupted jobs failed")
            while not stop_worker.is_set():
                try:
                    schedule_due(st)
                    if model_ready():
                        run_due_jobs(None, st, provider_for=lambda cid: metered(cid, "job"))
                    st.purge_expired_memory()
                    worker_state["last_tick"] = utcnow().isoformat()
                    worker_state["last_error"] = None
                except Exception as e:  # keep the worker alive; surface the problem in Settings
                    worker_state["last_error"] = friendly_error(e)
                    log.exception("worker tick failed")
                stop_worker.wait(worker_poll_seconds)
        finally:
            worker_state["running"] = False
            st.close()

    app.state.run_worker_once = lambda: run_due_jobs(None, open_store(), provider_for=lambda cid: metered(cid, "job"))
    return app
