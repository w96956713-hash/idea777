"""Triggers and background jobs.

- Schedule triggers: the worker keeps one pending job per schedule and creates the next one
  after each run.
- Event triggers: `fire_event` turns an event (e.g. a webhook payload) into a job.
- Agents can schedule their own follow-ups with the schedule_task tool.

Jobs run the agent's *deployed* version. Nobody is at a terminal during a job, so actions
that need approval are queued for a person (see runtime.execute_queued_action). Failed jobs
are retried with backoff and escalated after the last attempt. Retries are idempotent: each job
is an idempotency scope, so side effects that already happened in an earlier attempt (a record
created, a message queued) are not repeated.
"""

import time
from datetime import datetime, timedelta

from .runtime import AgentRuntime, queue_all
from .store import Store, utcnow

MAX_ATTEMPTS = 3


def schedule_due(store: Store, now: datetime | None = None) -> list[int]:
    """Make sure every schedule trigger of every deployed agent has its next job queued."""
    now = now or utcnow()
    created = []
    for agent in store.deployed_agents():
        spec, _ = store.load_spec(agent["name"], agent["deployed_version"])
        for i, trigger in enumerate(spec.triggers):
            if trigger.kind != "schedule":
                continue
            source = f"schedule:{i}"
            if store.pending_job_exists(agent["id"], source):
                continue
            last = store.last_job(agent["id"], source)
            due = datetime.fromisoformat(last["due_at"]) + timedelta(minutes=trigger.interval_minutes) if last else now
            text = trigger.input_template or trigger.description
            created.append(store.enqueue_job(agent["id"], text, source, max(due, now) if last else now))
    return created


def fire_event(store: Store, agent_name: str, event: str, payload: str) -> list[int]:
    """Queue a job for each of the agent's triggers listening to `event`."""
    spec, _ = store.deployed_spec(agent_name)
    agent_id = store.agent(agent_name)["id"]
    matching = [t for t in spec.triggers if t.kind == "event" and t.event == event]
    if not matching:
        raise ValueError(f"Agent {agent_name!r} has no trigger for event {event!r}")
    jobs = []
    for trigger in matching:
        template = trigger.input_template or "Event {event}:\n{payload}"
        text = template.replace("{payload}", payload).replace("{event}", event)
        jobs.append(store.enqueue_job(agent_id, text, f"event:{event}"))
    return jobs


def recover_interrupted_jobs(store: Store) -> list[int]:
    """Jobs left 'running' by a worker that stopped mid-run (crash, restart, deploy). Without this
    they would never run again and would block their schedule. They are retried like any failed
    attempt; the job's idempotency scope keeps actions from the interrupted attempt from being
    repeated. Call only when starting the single worker of a database."""
    recovered = []
    for job in store.conn.execute("SELECT * FROM jobs WHERE status = 'running'").fetchall():
        error = "Interrupted: the worker stopped during this job"
        if job["attempts"] < MAX_ATTEMPTS:
            store.update_job(job["id"], status="pending", due_at=utcnow().isoformat(), error=error)
        else:
            store.update_job(job["id"], status="failed", error=error)
            store.add_escalation(job["agent_id"], "Background job failed",
                                 f"Job #{job['id']} ({job['source']}) was interrupted on its last attempt.")
        recovered.append(job["id"])
    return recovered


def run_due_jobs(provider, store: Store, now: datetime | None = None, approver=queue_all,
                 provider_for=None) -> list[dict]:
    """Run due jobs. `provider_for(customer_id)` (optional) gives each organisation its own
    metered provider; otherwise `provider` is used for every job."""
    outcomes = []
    for job in store.due_jobs(now):
        agent = store.agent_by_id(job["agent_id"])
        if agent["deployed_version"] is None:
            store.update_job(job["id"], status="failed", error="agent is not deployed")
            outcomes.append({"job": job["id"], "status": "failed", "error": "agent is not deployed"})
            continue
        attempts = job["attempts"] + 1
        store.update_job(job["id"], status="running", attempts=attempts)
        spec, version = store.load_spec(agent["name"], agent["deployed_version"])
        try:
            job_provider = provider_for(agent["customer_id"]) if provider_for else provider
            runtime = AgentRuntime(job_provider, store, agent["id"], spec, version, approver=approver,
                                   trigger_source=job["source"], idempotency_scope=f"job:{job['id']}")
            result = runtime.run(job["input"])
            store.update_job(job["id"], status="done", run_id=result.run_id)
            outcomes.append({"job": job["id"], "status": "done", "run": result.run_id, "stop": result.stop_reason})
        except Exception as e:  # API outage, rate limit, bug: retry, then hand to a person
            error = getattr(e, "user_message", None) or f"{type(e).__name__}: {e}"
            if attempts < MAX_ATTEMPTS:
                retry_at = utcnow() + timedelta(minutes=2 ** attempts)
                store.update_job(job["id"], status="pending", due_at=retry_at.isoformat(), error=error)
                outcomes.append({"job": job["id"], "status": "retrying", "error": error})
            else:
                store.update_job(job["id"], status="failed", error=error)
                store.add_escalation(agent["id"], "Background job failed",
                                     f"Job #{job['id']} ({job['source']}) failed {attempts} times: {error}")
                outcomes.append({"job": job["id"], "status": "failed", "error": error})
    return outcomes


def work(provider, store: Store, once: bool = False, poll_seconds: int = 30, log=print, provider_for=None,
         stop=None) -> None:
    for job_id in recover_interrupted_jobs(store):
        log({"job": job_id, "status": "recovered after interruption"})
    while True:
        schedule_due(store)
        for outcome in run_due_jobs(provider, store, provider_for=provider_for):
            log(outcome)
        store.purge_expired_memory()
        if once or (stop is not None and stop.wait(poll_seconds)):
            return
        if stop is None:
            time.sleep(poll_seconds)
