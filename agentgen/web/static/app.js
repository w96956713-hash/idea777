import {
  api, h, setChildren, fmt, pageHead, card, badge, statusBadge, notice, table, field, kv, list, stat, progress, toast,
  actionButton, waitForOperation, operationView, setUnauthorizedHandler,
} from "./ui.js";

const state = { me: null, renderToken: 0 };
const root = document.getElementById("app");
let view = null;

const isAdmin = () => state.me?.user.role === "admin";
const go = (hash) => { if (location.hash === hash) render(); else location.hash = hash; };

// ---------------------------------------------------------------- boot, auth, shell

setUnauthorizedHandler(() => { state.me = null; renderAuth(); });

async function boot() {
  try {
    state.me = await api("GET", "/me");
  } catch {
    state.me = null;
  }
  if (!state.me) return renderAuth();
  renderShell();
  render();
}

function renderAuth(mode = "signin") {
  const msg = h("div");
  const email = h("input", { type: "email", id: "auth-email", autocomplete: "email", required: true });
  const password = h("input", { type: "password", id: "auth-password", autocomplete: mode === "signin" ? "current-password" : "new-password", required: true });
  const org = h("input", { type: "text", id: "auth-org", autocomplete: "organization", required: true });
  const name = h("input", { type: "text", id: "auth-name", autocomplete: "name", required: true });
  const form = h("form", { class: "stack" },
    mode === "signup" ? field("Organisation", org, "your company") : null,
    mode === "signup" ? field("Your name", name) : null,
    field("Email", email),
    field("Password", password, mode === "signup" ? "at least 10 characters" : null),
    msg,
    h("button", { class: "primary", type: "submit" }, mode === "signup" ? "Create account" : "Sign in"));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    msg.replaceChildren();
    try {
      if (mode === "signup") {
        await api("POST", "/auth/signup", { organization: org.value, name: name.value, email: email.value, password: password.value });
      } else {
        await api("POST", "/auth/login", { email: email.value, password: password.value });
      }
      await boot();
    } catch (err) {
      msg.replaceChildren(notice("bad", err.message));
    }
  });
  root.replaceChildren(h("div", { class: "auth" }, h("div", { class: "card" },
    h("div", {}, h("div", { class: "brand-big", text: "agentgen" }),
      h("p", { class: "muted", text: "Describe a business problem. Get a tested AI agent built for it." })),
    form,
    h("p", { class: "small muted" }, mode === "signup" ? "Already have an account? " : "New organisation? ",
      h("button", { class: "link", type: "button", onclick: () => renderAuth(mode === "signup" ? "signin" : "signup") },
        mode === "signup" ? "Sign in" : "Create an account")))));
  (mode === "signup" ? org : email).focus();
}

const NAV = [
  ["Work", null],
  ["Overview", "#/"],
  ["Projects", "#/projects"],
  ["Create an agent", "#/projects/new"],
  ["Agents", null],
  ["Agent configuration", "#/agents"],
  ["Testing", "#/testing"],
  ["Deployed agents", "#/deployed"],
  ["Operations", null],
  ["Execution history", "#/runs"],
  ["Approvals", "#/approvals"],
  ["Monitoring", "#/monitoring"],
  ["Setup", null],
  ["Data & integrations", "#/data"],
  ["Settings", "#/settings"],
];

function renderShell() {
  const nav = h("nav", { class: "nav", "aria-label": "Main" },
    NAV.map(([label, href]) => href ? h("a", { href, "data-href": href }, h("span", { text: label }),
      href === "#/approvals" ? h("span", { class: "count", id: "nav-approvals", hidden: true }) : null)
      : h("div", { class: "group", text: label })));
  const shell = h("div", { class: "shell" },
    h("aside", { class: "sidebar" },
      h("div", { class: "brand" }, "agentgen", h("small", { text: "AI agent platform" })),
      nav,
      h("div", { class: "account" }, h("b", { text: state.me.organization }), `${state.me.user.name} · ${state.me.user.role}`,
        h("div", {}, actionButton("Sign out", async () => { await api("POST", "/auth/logout"); state.me = null; renderAuth(); }, { small: true })))),
    h("div", { class: "main" },
      h("div", { class: "topbar" }, h("button", { class: "small", type: "button", onclick: () => shell.classList.toggle("menu-open") }, "Menu"),
        h("b", { text: "agentgen" })),
      view = h("div", { class: "content", id: "view" })));
  nav.addEventListener("click", (e) => { if (e.target.closest("a")) shell.classList.remove("menu-open"); });
  root.replaceChildren(shell);
}

function highlightNav() {
  const hash = location.hash || "#/";
  let best = null;
  for (const a of document.querySelectorAll(".nav a")) {
    const href = a.dataset.href;
    const match = href === "#/" ? hash === "#/" || hash === "" : hash === href || hash.startsWith(href + "/");
    if (match && (!best || href.length > best.dataset.href.length)) best = a;
  }
  document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("active", a === best));
  if (hash.startsWith("#/projects/new")) {
    document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.href === "#/projects/new"));
  }
}

async function refreshApprovalCount() {
  try {
    const a = await api("GET", "/approvals");
    const el = document.getElementById("nav-approvals");
    if (el) { el.textContent = String(a.actions.length); el.hidden = a.actions.length === 0; }
  } catch { /* shown elsewhere */ }
}

// ---------------------------------------------------------------- router

const ROUTES = [
  [/^#?\/?$/, pageOverview],
  [/^#\/projects$/, pageProjects],
  [/^#\/projects\/new$/, pageNewProject],
  [/^#\/projects\/(\d+)$/, pageProject],
  [/^#\/agents$/, pageAgents],
  [/^#\/agents\/(\d+)(?:\/([a-z]+))?(?:\/v(\d+))?$/, pageAgent],
  [/^#\/testing$/, pageTesting],
  [/^#\/deployed$/, pageDeployed],
  [/^#\/runs$/, pageRuns],
  [/^#\/runs\/(\d+)$/, pageRun],
  [/^#\/approvals$/, pageApprovals],
  [/^#\/monitoring$/, pageMonitoring],
  [/^#\/data$/, pageData],
  [/^#\/settings$/, pageSettings],
];

async function render() {
  if (!state.me || !view) return;
  const token = ++state.renderToken;
  const isCurrent = () => token === state.renderToken;
  const hash = location.hash || "#/";
  highlightNav();
  refreshApprovalCount();
  const route = ROUTES.find(([re]) => re.test(hash));
  if (!route) { view.replaceChildren(pageHead("Not found", "This page doesn't exist.")); return; }
  const params = hash.match(route[0]).slice(1);
  if (!view.childElementCount) view.replaceChildren(h("p", { class: "muted", text: "Loading…" }));
  try {
    const nodes = await route[1]({ params, isCurrent });
    if (isCurrent()) { view.replaceChildren(...[nodes].flat().filter(Boolean)); window.scrollTo?.(0, 0); }
  } catch (e) {
    if (isCurrent() && e.status !== 401) view.replaceChildren(pageHead("Something went wrong"), notice("bad", e.message));
  }
}

window.addEventListener("hashchange", render);

// ---------------------------------------------------------------- shared pieces

function modelNotice(configured) {
  if (configured) return null;
  return notice("warn", h("b", { text: "No model API key is configured on this server." }),
    "Generating questions, analysing requirements, generating agents, testing and running them need the Claude API. " +
    "Set ANTHROPIC_API_KEY in the server's environment and restart. Everything else works without it.");
}

function runsTable(runs, { showAgent = true } = {}) {
  return table([
    { label: "Run", render: (r) => h("a", { href: `#/runs/${r.id}`, text: `#${r.id}` }) },
    showAgent ? { label: "Agent", class: "nowrap", render: (r) => h("a", { href: `#/agents/${r.agent_id}`, text: r.agent }) } : null,
    { label: "Version", render: (r) => `v${r.version}` },
    { label: "Source", render: (r) => sourceBadge(r.source) },
    { label: "Status", render: (r) => statusBadge(r.status) },
    { label: "Task", class: "wrap", render: (r) => r.input.length > 90 ? r.input.slice(0, 90) + "…" : r.input },
    { label: "Duration", class: "nowrap", render: (r) => fmt.ms(r.duration_ms) },
    { label: "Tokens", class: "nowrap", render: (r) => `${fmt.tokens(r.input_tokens)} / ${fmt.tokens(r.output_tokens)}` },
    { label: "Cost", render: (r) => fmt.usd(r.cost_usd) },
    { label: "Started", class: "nowrap", render: (r) => fmt.ago(r.created_at) },
  ].filter(Boolean), runs, { empty: "No runs yet.", onRow: (r) => go(`#/runs/${r.id}`) });
}

function sourceBadge(source) {
  if (source === "sandbox") return badge("sandbox", "info");
  if (source === "manual") return badge("live", "accent");
  if (source?.startsWith("schedule")) return badge("schedule", "accent");
  if (source?.startsWith("event")) return badge(source.replace("event:", "event: "), "accent");
  if (source === "agent") return badge("follow-up", "accent");
  return badge(source || "—");
}

function riskBadge(risk) {
  const kind = { read: "", write_internal: "info", write_external: "warn", external_comm: "warn", financial: "bad" }[risk];
  return badge(risk.replace("_", " "), kind);
}

// ---------------------------------------------------------------- overview

async function pageOverview() {
  const o = await api("GET", "/overview");
  const budgetUse = o.monthly_budget_usd ? o.month_spend_usd / o.monthly_budget_usd : 0;
  return [
    pageHead("Overview", `${state.me.organization}: projects, agents and activity.`,
      [h("a", { class: "btn primary", href: "#/projects/new" }, "Create an agent")]),
    modelNotice(o.model_configured),
    h("div", { class: "stats" },
      stat("Projects", o.projects), stat("Agents", o.agents), stat("Deployed", o.deployed),
      stat("Runs, last 7 days", o.runs_7d), stat("Failed runs, 7 days", o.failed_runs_7d, o.failed_runs_7d > 0),
      stat("Approvals waiting", o.pending_approvals, o.pending_approvals > 0),
      stat("Model spend this month", `${fmt.usd(o.month_spend_usd)} / ${fmt.usd(o.monthly_budget_usd)}`, budgetUse >= 0.8)),
    o.running_operations.length ? card("In progress", null, list(o.running_operations.map((op) =>
      `${op.kind} started ${fmt.ago(op.created_at)} by ${op.created_by}`))) : null,
    o.agents_needing_attention.length ? notice("warn", h("b", { text: "Agents whose latest version failed its tests" }),
      list(o.agents_needing_attention.map((a) => h("a", { href: `#/agents/${a.id}/tests`, text: a.name })))) : null,
    card("Recent runs", "Live, scheduled, event-triggered and sandbox runs.", [runsTable(o.recent_runs)],
      [h("a", { href: "#/runs" }, "All runs")]),
    o.agents === 0 && o.projects === 0 ? card("Get started", null, [h("ol", { class: "steps" },
      h("li", {}, h("a", { href: "#/data", text: "Upload your data" }), " (CSV exports, documents) so agents can use it."),
      h("li", {}, h("a", { href: "#/projects/new", text: "Describe a business problem" }), "."),
      h("li", {}, "Answer the follow-up questions, review the analysis and generate the agent."),
      h("li", {}, "Test it, try it on a task, approve and deploy it."))]) : null,
  ];
}

// ---------------------------------------------------------------- projects

async function pageProjects() {
  const projects = await api("GET", "/projects");
  return [
    pageHead("Projects", "Each project turns one business problem into an agent.",
      [h("a", { class: "btn primary", href: "#/projects/new" }, "New project")]),
    table([
      { label: "Project", render: (p) => h("a", { href: `#/projects/${p.id}`, text: p.title }) },
      { label: "Stage", render: (p) => statusBadge(p.stage) },
      { label: "Agent", render: (p) => p.agent_name || "—" },
      { label: "Updated", render: (p) => fmt.ago(p.updated_at) },
    ], projects, { empty: "No projects yet. Create one to describe a problem.", onRow: (p) => go(`#/projects/${p.id}`) }),
  ];
}

const INTAKE = [
  ["title", "Project name", "text", "e.g. Weekly sales reporting", true],
  ["company", "Company", "text", "", true],
  ["industry", "Industry", "text", "", false],
  ["problem", "The problem you want to solve", "area", "What takes time, goes wrong or gets missed today? At least 20 characters.", true],
  ["workflow", "How the work is done today", "area", "Steps, people involved, volumes, timing.", false],
  ["outcome", "Desired outcome", "area", "What does success look like? How would you measure it?", false],
  ["systems", "Existing software and data sources", "area", "Systems, exports, APIs, spreadsheets, documents.", false],
  ["constraints", "Operational and security constraints", "area", "Who may see what, what must never happen, approvals.", false],
];

function intakeForm(values, submitLabel, onSubmit) {
  const inputs = {};
  const msg = h("div");
  const form = h("form", { class: "form-grid" },
    INTAKE.map(([key, label, type, help, required]) => {
      const input = type === "area"
        ? h("textarea", { id: `intake-${key}`, rows: key === "problem" ? 5 : 3, required, value: values[key] || "" })
        : h("input", { type: "text", id: `intake-${key}`, required, value: values[key] || "" });
      inputs[key] = input;
      return h("div", { class: type === "area" ? "full" : "" }, field(label + (required ? " *" : ""), input, help));
    }),
    h("div", { class: "full stack" }, msg, h("div", { class: "row" }, h("button", { class: "primary", type: "submit" }, submitLabel))));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    msg.replaceChildren();
    const body = Object.fromEntries(Object.entries(inputs).map(([k, el]) => [k, el.value.trim()]));
    try { await onSubmit(body); } catch (err) { msg.replaceChildren(notice("bad", err.message)); }
  });
  return form;
}

async function pageNewProject() {
  const data = await api("GET", "/data");
  return [
    pageHead("Create an agent", "Describe the problem in your own words. Next, the platform asks follow-up questions, " +
      "analyses what can be automated safely, and designs an agent for you to test and approve.", [], "Projects"),
    data.datasets.length || data.documents.length ? null : notice("info",
      "Tip: upload the data the agent should work with first (", h("a", { href: "#/data", text: "Data & integrations" }),
      "). The designer only connects agents to data that is actually available."),
    card(null, null, [intakeForm({}, "Save and continue", async (body) => {
      const p = await api("POST", "/projects", body);
      toast("Project saved");
      go(`#/projects/${p.id}`);
    })]),
  ];
}

async function pageProject({ params: [id], isCurrent }) {
  const [p, data, me] = await Promise.all([api("GET", `/projects/${id}`), api("GET", "/data"), api("GET", "/me")]);
  const latest = (kind) => p.operations.find((o) => o.kind === kind);
  const running = p.operations.find((o) => o.status === "queued" || o.status === "running");
  if (running) {
    waitForOperation(running, null, isCurrent).then((op) => {
      if (!isCurrent()) return;
      if (op.status === "failed") toast(`${op.kind} failed: ${op.error}`, "bad");
      render();
    });
  }
  const start = (path, label) => actionButton(label, async () => {
    const op = await api("POST", `/projects/${id}/${path}`);
    if (op.status === "failed") throw new Error(op.error);
    render();
  }, { kind: "primary", disabled: !!running || !me.model_configured });

  const opStatus = (kind, label) => operationView(latest(kind), label);
  const steps = [
    ["Requirements", true],
    ["Follow-up questions", p.questions.length > 0],
    ["Analysis", !!p.analysis],
    ["Review", !!p.amendments],
    ["Generated agent", !!p.agent],
  ];

  // 1. requirements
  let editing = false;
  const reqBody = h("div");
  const showReq = () => reqBody.replaceChildren(editing
    ? intakeForm({ ...p.intake, title: p.title }, "Save changes", async (body) => {
      await api("PUT", `/projects/${id}/intake`, body);
      toast("Requirements updated");
      render();
    })
    : kv(INTAKE.filter(([k]) => k !== "title").map(([k, label]) => [label, p.intake[k] || "—"])));
  showReq();
  const reqCard = card(stepTitle(1, "Requirements", true), "What the company told us.", [reqBody],
    [h("button", { type: "button", onclick: (e) => { editing = !editing; e.target.textContent = editing ? "Cancel" : "Edit"; showReq(); } }, "Edit")]);

  // 2. data
  const dataCard = card(stepTitle(2, "Data available to the agent", data.datasets.length + data.documents.length > 0),
    "The designer connects the agent to these exact datasets and documents.", [
      data.datasets.length || data.documents.length
        ? list([...data.datasets.map((d) => `Dataset ${d.name}: ${d.rows} rows (${d.columns.join(", ")})`),
          ...data.documents.map((d) => `Document “${d.title}”`)])
        : notice("info", "No data uploaded yet. If the agent should work with company data, upload it before generating."),
    ], [h("a", { href: "#/data" }, "Manage data")]);

  // 3. questions
  const answerInputs = [];
  const answersMap = Object.fromEntries(p.answers.map((a) => [a.question, a.answer]));
  const qBody = p.questions.length
    ? h("form", { class: "stack", onsubmit: async (e) => {
      e.preventDefault();
      try {
        await api("PUT", `/projects/${id}/answers`, { answers: answerInputs.map(([q, el]) => ({ question: q, answer: el.value })) });
        toast("Answers saved");
        render();
      } catch (err) { toast(err.message, "bad"); }
    } },
    p.questions.map((q, i) => {
      const input = h("textarea", { id: `answer-${i}`, rows: 2, value: answersMap[q.question] || "" });
      answerInputs.push([q.question, input]);
      return field(`${i + 1}. ${q.question}`, input, q.why);
    }),
    h("div", { class: "row" }, h("button", { class: "primary", type: "submit" }, "Save answers"),
      h("span", { class: "small muted", text: "Leave a question blank if you don't know; it is treated as unknown." })))
    : h("p", { class: "muted", text: "Generate targeted questions about the process, data, rules and risks before anything is designed." });
  const qCard = card(stepTitle(3, "Follow-up questions", p.questions.length > 0),
    "Answers sharpen the analysis and the agent design.", [opStatus("questions", "Generating questions"), qBody],
    [start("questions", p.questions.length ? "Regenerate questions" : "Generate questions")]);

  // 4. analysis
  const a = p.analysis;
  const aBody = a ? analysisView(a, p.cost_estimate) : h("p", { class: "muted", text: "Analyse which steps can be automated safely, which should stay with people, which systems are needed and what it costs." });
  const aCard = card(stepTitle(4, "Analysis", !!a), "What to automate, what stays human, integrations and risks.",
    [opStatus("analysis", "Analysing"), aBody], [start("analysis", a ? "Re-analyse" : "Analyse requirements")]);

  // 5. review
  const amend = h("textarea", { id: "amendments", rows: 4, value: p.amendments });
  const rCard = card(stepTitle(5, "Review and corrections", !!p.amendments),
    "Correct anything the analysis got wrong, or add decisions. These override everything above when the agent is designed.", [
      field("Corrections and decisions", amend, "e.g. “Reports go to management only”, “Ignore the CRM for now”"),
      h("div", { class: "row" }, actionButton("Save corrections", async () => {
        await api("PUT", `/projects/${id}/amendments`, { amendments: amend.value });
        toast("Corrections saved");
        render();
      })),
    ]);

  // 6. generate
  const gBody = p.agent
    ? notice("ok", h("b", { text: `Agent ${p.agent.name} generated.` }), h("span", {},
      "Review its configuration and run its tests: ", h("a", { href: `#/agents/${p.agent.id}`, text: "open the agent" }), "."))
    : h("p", { class: "muted", text: a ? "Ready to design the agent from the requirements, answers, analysis, corrections and available data."
      : "Analysing first is recommended; you can also generate directly from the requirements." });
  const gCard = card(stepTitle(6, "Generate the agent", !!p.agent),
    "Creates a new, versioned agent definition. Generating again creates a new version of the same agent.",
    [opStatus("design", "Designing the agent"), gBody],
    [start("design", p.agent ? "Generate a new version" : "Generate agent")]);

  return [
    pageHead(p.title, null, [statusBadge(p.stage)], h("a", { href: "#/projects", text: "Projects" })),
    modelNotice(me.model_configured),
    h("div", { class: "row small muted" }, steps.map(([label, done], i) => h("span", {},
      h("span", { class: `step-num${done ? " done" : ""}`, text: String(i + 1) }), label))),
    reqCard, dataCard, qCard, aCard, rCard, gCard,
  ];
}

function stepTitle(n, label, done) {
  return h("h2", {}, h("span", { class: `step-num${done ? " done" : ""}`, text: String(n) }), label);
}

function analysisView(a, estimate) {
  return h("div", { class: "stack" },
    kv([["Process today", a.process_summary], ["Recommended agent", a.recommended_agent],
      ["Complexity", h("span", {}, badge(a.complexity, a.complexity === "high" ? "warn" : "info"), " ", a.complexity_reasons.join("; "))],
      ["Estimated volume", `${a.estimated_runs_per_month} runs per month`],
      estimate ? ["Estimated model cost", `${fmt.usd(estimate.usd_per_month)} per month (${fmt.usd(estimate.usd_per_run)} per run; ${estimate.basis})`] : null]),
    h("h3", { text: "Capabilities" }), list(a.recommended_capabilities),
    h("h3", { text: "Opportunities" }),
    table([
      { label: "Opportunity", class: "wrap", render: (o) => h("div", {}, h("b", { text: o.title }), h("div", { class: "small muted", text: o.description })) },
      { label: "Automate", class: "wrap", render: (o) => list(o.automatable_steps) },
      { label: "Keep human", class: "wrap", render: (o) => list(o.human_steps) },
      { label: "Value", class: "wrap", key: "value" },
      { label: "Risk", render: (o) => badge(o.risk, o.risk === "high" ? "bad" : o.risk === "medium" ? "warn" : "ok") },
      { label: "", render: (o) => o.recommended ? badge("recommended", "ok") : badge("not now") },
    ], a.opportunities),
    h("h3", { text: "Stays with people" }), list(a.keep_human),
    h("h3", { text: "Systems and integrations" }),
    table([
      { label: "System", key: "system" }, { label: "Purpose", class: "wrap", key: "purpose" },
      { label: "Access", key: "access" }, { label: "Status", render: (i) => statusBadge(i.status) },
      { label: "How", class: "wrap", key: "approach" },
    ], a.integrations, { empty: "No integrations identified." }),
    h("h3", { text: "Data needed" }), list(a.data_needed),
    h("h3", { text: "Risks" }), list(a.risks),
    a.open_questions.length ? notice("warn", h("b", { text: "Open questions" }), list(a.open_questions)) : null);
}

// ---------------------------------------------------------------- agents

async function pageAgents() {
  const agents = await api("GET", "/agents");
  return [
    pageHead("Agents", "Generated agents and their versions. Open one to configure, test, run and deploy it."),
    table([
      { label: "Agent", class: "wrap", render: (a) => h("div", {}, h("a", { href: `#/agents/${a.id}`, text: a.name }), h("div", { class: "small muted", text: a.purpose })) },
      { label: "Latest", render: (a) => h("span", { class: "nowrap" }, `v${a.latest_version} `, statusBadge(a.latest_status)) },
      { label: "Tests", render: (a) => a.latest_test ? `${a.latest_test.passed_count}/${a.latest_test.total}` : "not run" },
      { label: "Deployed", render: (a) => a.deployed_version ? badge(`v${a.deployed_version}`, "ok") : "—" },
      { label: "Tools", key: "tools" },
      { label: "Last run", render: (a) => fmt.ago(a.last_run_at) },
    ], agents, { empty: "No agents yet. Create a project to generate one.", onRow: (a) => go(`#/agents/${a.id}`) }),
  ];
}

const AGENT_TABS = [["overview", "Overview"], ["config", "Configuration"], ["tests", "Tests"], ["run", "Run a task"],
  ["deploy", "Versions & deployment"], ["activity", "Activity"]];

async function pageAgent({ params: [id, tab = "overview", v], isCurrent }) {
  const base = await api("GET", `/agents/${id}`);
  const ver = v ? await api("GET", `/agents/${id}/versions/${v}`) : base;
  const version = ver.version;
  const link = (t, vv = version) => `#/agents/${id}/${t}${vv !== base.version ? `/v${vv}` : ""}`;
  const select = h("select", { id: "version-select", "aria-label": "Version" },
    base.versions.map((x) => h("option", { value: x.version, selected: x.version === version, text: `Version ${x.version} · ${x.status}${x.version === base.agent.deployed_version ? " · deployed" : ""}` })));
  select.addEventListener("change", () => go(`#/agents/${id}/${tab}${Number(select.value) !== base.version ? `/v${select.value}` : ""}`));
  const tabs = h("nav", { class: "tabs" }, AGENT_TABS.map(([key, label]) => h("a", { href: link(key), class: key === tab ? "active" : "" }, label)));
  const head = pageHead(base.agent.name, ver.spec.purpose, [
    statusBadge(ver.status), base.agent.deployed_version ? badge(`deployed v${base.agent.deployed_version}`, "ok") : badge("not deployed"),
    select], h("a", { href: "#/agents", text: "Agents" }));
  const body = await {
    overview: tabOverview, config: tabConfig, tests: tabTests, run: tabRun, deploy: tabDeploy, activity: tabActivity,
  }[tab]?.({ id, base, ver, version, isCurrent }) ?? notice("bad", "Unknown tab");
  return [head, tabs, body].flat();
}

function tabOverview({ base, ver }) {
  const s = ver.spec;
  return [
    s.open_questions.length ? notice("warn", h("b", { text: "Open questions and validation notes" }), list(s.open_questions)) : null,
    h("div", { class: "grid-2" },
      card("Responsibilities", null, [list(s.capabilities)]),
      card("Limits", null, [kv([["Model", s.model], ["Effort", s.effort], ["Max turns per run", s.limits.max_turns],
        ["Token budget per run", s.limits.max_tokens_per_run.toLocaleString()],
        ["Memory", s.memory.enabled ? `On: ${s.memory.what_to_remember} (${s.memory.retention_days} days)` : "Off"]])])),
    card("Workflow", null, [s.workflow.length ? h("ol", { class: "steps" }, s.workflow.map((w) =>
      h("li", {}, h("b", { text: w.step }), `: ${w.description}`, h("div", { class: "small muted", text: `If this fails: ${w.on_failure}` }))))
      : h("p", { class: "muted", text: "No workflow steps defined." })]),
    card("Tools and permissions", "Agents can only use these tools. Approval rules are enforced by the platform.", [
      table([
        { label: "Tool", class: "wrap", render: (t) => h("div", {}, h("b", { text: t.name }), h("div", { class: "small muted", text: t.purpose })) },
        { label: "Capability", render: (t) => t.component },
        { label: "Risk", render: (t) => riskBadge(t.risk) },
        { label: "Approval", render: (t) => t.requires_approval ? badge(t.approval_forced ? "always required" : "required", "warn") : badge("automatic") },
        { label: "Integration", class: "wrap", render: (t) => h("div", {}, badge(t.integration.label, t.integration.kind === "built_in" ? "" : "info"), h("div", { class: "small muted", text: t.integration.status })) },
      ], ver.tools)]),
    h("div", { class: "grid-2" },
      card("Data sources", null, [table([
        { label: "Source", key: "name" }, { label: "Kind", key: "kind" },
        { label: "Status", render: (d) => statusBadge(d.status) }, { label: "Description", class: "wrap", key: "description" },
      ], s.data_sources, { empty: "None." })]),
      card("Triggers", null, [list(s.triggers.map((t) => `${t.kind}${t.kind === "schedule" ? ` every ${t.interval_minutes} min` : ""}${t.event ? ` on “${t.event}”` : ""}: ${t.description}`))])),
    h("div", { class: "grid-2" },
      card("Success criteria", null, [list(s.success_criteria)]),
      card("Never", "Hard restrictions in the agent's instructions.", [list(s.restrictions)])),
    card("Instructions", "What the agent is told.", [h("pre", { text: s.instructions })]),
    s.assumptions.length ? card("Assumptions", null, [list(s.assumptions)]) : null,
    base.project ? h("p", { class: "small muted" }, "Generated from project ", h("a", { href: `#/projects/${base.project.id}`, text: base.project.title }), ".") : null,
  ];
}

function tabConfig({ id, ver, version }) {
  const s = ver.spec;
  const toolRows = ver.tools.map((t) => {
    const approval = h("input", { type: "checkbox", checked: t.requires_approval, disabled: t.approval_forced, id: `approve-${t.name}` });
    const inputs = Object.entries(t.config_keys).map(([key, desc]) => {
      const input = h("input", { type: "text", value: t.config[key] || "", id: `cfg-${t.name}-${key}`, placeholder: t.required_config.includes(key) ? "required" : "" });
      return [key, input, field(key, input, desc)];
    });
    return { t, approval, inputs };
  });
  const maxTurns = h("input", { type: "number", id: "max-turns", min: 3, max: 50, value: s.limits.max_turns });
  const maxTokens = h("input", { type: "number", id: "max-tokens", min: 20000, max: 1000000, step: 1000, value: s.limits.max_tokens_per_run });
  const model = h("select", { id: "model" }, ver.models.map((m) => h("option", { value: m, selected: m === s.model, text: m })));
  const effort = h("select", { id: "effort" }, ["low", "medium", "high", "xhigh", "max"].map((e) => h("option", { value: e, selected: e === s.effort, text: e })));
  const instructions = h("textarea", { id: "instructions", rows: 12, value: s.instructions });
  const notes = h("div");
  const save = actionButton("Save as new version", async () => {
    const body = {
      tools: toolRows.map(({ t, approval, inputs }) => ({ name: t.name, requires_approval: approval.checked,
        config: Object.fromEntries(inputs.map(([k, el]) => [k, el.value.trim()])) })),
      max_turns: Number(maxTurns.value), max_tokens_per_run: Number(maxTokens.value), model: model.value, effort: effort.value,
      instructions: instructions.value,
    };
    const r = await api("PUT", `/agents/${id}/versions/${version}/config`, body);
    toast(`Saved as version ${r.version}. Run its tests before deploying.`);
    go(`#/agents/${id}/config/v${r.version}`);
  }, { kind: "primary" });

  const yaml = h("textarea", { id: "spec-yaml", rows: 24, class: "mono" });
  const yamlBox = h("details", { ontoggle: async (e) => {
    if (e.target.open && !yaml.value) yaml.value = (await api("GET", `/agents/${id}/versions/${version}/yaml`)).yaml;
  } }, h("summary", { text: "Edit the full definition (YAML)" }), h("div", { class: "stack" },
    yaml, h("div", { class: "row" }, actionButton("Save YAML as new version", async () => {
      const r = await api("PUT", `/agents/${id}/spec`, { yaml: yaml.value });
      toast(`Saved as version ${r.version}`);
      go(`#/agents/${id}/config/v${r.version}`);
    }))));

  return [
    notice("info", `Editing version ${version}. Saving creates a new version, which must pass its tests before it can be approved or deployed. ` +
      "Approval for external writes, external messages and payments can't be switched off."),
    card("Tools", "Which capabilities the agent has, their settings and whether each call needs a person's approval.", [
      h("div", { class: "stack" }, toolRows.map(({ t, approval, inputs }) => h("div", { class: "card" },
        h("div", { class: "row between" }, h("div", {}, h("b", { text: t.name }), " ", h("span", { class: "muted small", text: t.component }), " ", riskBadge(t.risk)),
          h("label", { class: "check" }, approval, t.approval_forced ? "Approval always required" : "Require approval for each call")),
        h("p", { class: "small muted", text: `${t.summary} ${t.integration.status}` }),
        inputs.length ? h("div", { class: "form-grid" }, inputs.map(([, , f]) => f)) : null)))]),
    card("Execution limits and model", null, [h("div", { class: "form-grid" },
      field("Max model turns per run", maxTurns), field("Token budget per run", maxTokens, "input + output"),
      field("Model", model), field("Effort", effort, "higher = more thorough, slower, costlier"))]),
    card("Instructions", null, [instructions]),
    notes, h("div", { class: "row" }, save),
    card(null, null, [yamlBox]),
  ];
}

function testCase(c) {
  return h("div", { class: `case ${c.passed ? "pass" : "fail"}` },
    h("div", { class: "row between" }, h("div", { class: "row" }, h("b", { text: c.name }), badge(c.category.replaceAll("_", " "), ["permission", "unauthorized_action"].includes(c.category) ? "warn" : "info"),
      c.carried ? badge("regression suite") : null), c.passed ? badge("passed", "ok") : badge("failed", "bad")),
    c.error ? notice("bad", c.error) : null,
    kv([["Tool check", c.tool_check], ["Grader", c.judge_reasoning || "—"],
      ["Tokens / cost", `${fmt.tokens(c.input_tokens)} in, ${fmt.tokens(c.output_tokens)} out · ${fmt.usd(c.usd)}`]]),
    c.transcript ? h("details", {}, h("summary", { text: "What the agent did" }), h("pre", { text: c.transcript })) : null);
}

async function tabTests({ id, ver, version, isCurrent }) {
  const [reports, me] = await Promise.all([api("GET", `/agents/${id}/versions/${version}/tests`), api("GET", "/me")]);
  const latest = reports[0]?.report;
  const opBox = h("div");
  const runBtn = actionButton("Run test suite", async () => {
    let op = await api("POST", `/agents/${id}/versions/${version}/test`);
    op = await waitForOperation(op, (o) => setChildren(opBox, operationView(o, "Running tests")), isCurrent);
    if (op.status === "failed") toast(`Tests failed to run: ${op.error}`, "bad");
    if (isCurrent()) render();
  }, { kind: "primary", disabled: !me.model_configured });
  const cases = ver.spec.test_cases;
  const summary = latest ? h("div", { class: "stack" },
    h("div", { class: "row" }, latest.passed ? badge("Passed", "ok") : badge("Did not pass", "bad"),
      h("span", { text: `${latest.cases.filter((c) => c.passed).length} of ${latest.cases.length} cases passed (${fmt.pct(latest.pass_rate)}, threshold ${fmt.pct(latest.threshold)})` }),
      h("span", { class: "muted", text: `· ${fmt.ago(reports[0].created_at)} · avg ${fmt.usd(latest.avg_usd_per_run)} per run` })),
    progress(latest.pass_rate, latest.passed ? "" : "bad"),
    latest.blockers.length ? notice("bad", h("b", { text: "Why it can't be approved yet" }), list(latest.blockers)) : notice("ok", "All gates passed: pass rate, safety and permission cases, no regressions."),
    latest.over_budget?.length ? notice("warn", `Over the token budget: ${latest.over_budget.join(", ")}`) : null)
    : h("p", { class: "muted", text: "This version hasn't been tested yet." });
  return [
    modelNotice(me.model_configured),
    card(`Test results · version ${version}`, "Each case runs on a copy of your data with approvals granted. External systems are never called: they're simulated. " +
      "Safety and permission cases must all pass before a version can be approved.", [opBox, summary], [runBtn]),
    latest ? card("Cases", null, [h("div", { class: "stack" }, latest.cases.map(testCase))]) : card("Planned test cases", null, [table([
      { label: "Case", key: "name" }, { label: "Category", render: (c) => badge(c.category.replaceAll("_", " ")) },
      { label: "Input", class: "wrap", render: (c) => c.input.slice(0, 160) }, { label: "Expected", class: "wrap", key: "expected_behavior" },
      { label: "Simulated", render: (c) => [c.fail_tools.length ? `outage: ${c.fail_tools.join(", ")}` : "", c.fixtures.length ? `fixtures: ${c.fixtures.map((f) => f.tool).join(", ")}` : ""].filter(Boolean).join("; ") || "—" },
    ], cases)]),
    reports.length > 1 ? card("Earlier test runs", null, [table([
      { label: "When", render: (r) => fmt.date(r.created_at) },
      { label: "Result", render: (r) => r.report.passed ? badge("passed", "ok") : badge("failed", "bad") },
      { label: "Pass rate", render: (r) => fmt.pct(r.report.pass_rate) },
      { label: "Blockers", class: "wrap", render: (r) => r.report.blockers.join("; ") || "—" },
    ], reports.slice(1))]) : null,
  ];
}

async function tabRun({ id, base, ver, version, isCurrent }) {
  const me = await api("GET", "/me");
  const deployed = base.agent.deployed_version;
  const task = h("textarea", { id: "run-task", rows: 4, placeholder: "Describe a task for the agent, e.g. a question or a request it would receive." });
  const modeSandbox = h("input", { type: "radio", name: "mode", value: "sandbox", checked: true, id: "mode-sandbox" });
  const modeLive = h("input", { type: "radio", name: "mode", value: "live", disabled: !deployed, id: "mode-live" });
  const result = h("div", { class: "stack" });
  const samples = ver.spec.test_cases.filter((c) => !["permission", "unauthorized_action", "api_failure"].includes(c.category)).slice(0, 5);
  const runBtn = actionButton("Run", async () => {
    if (!task.value.trim()) throw new Error("Write a task first.");
    const mode = modeLive.checked ? "live" : "sandbox";
    result.replaceChildren();
    let op = await api("POST", `/agents/${id}/runs`, { task: task.value, mode, version: mode === "sandbox" ? version : undefined });
    op = await waitForOperation(op, (o) => setChildren(result, operationView(o, "Agent is working")), isCurrent);
    if (!isCurrent()) return;
    const runId = op.result?.run_id;
    const run = runId ? await api("GET", `/runs/${runId}`) : null;
    setChildren(result,
      op.status === "failed" ? notice("bad", h("b", { text: "The run failed" }), op.error) : null,
      run ? runDetail(run, { compact: true }) : null);
  }, { kind: "primary", disabled: !me.model_configured });
  return [
    modelNotice(me.model_configured),
    card("Run a task", "See exactly what the agent does: every tool call, its input and result, and the final answer.", [
      field("Task", task),
      samples.length ? h("div", { class: "chips" }, h("span", { class: "small muted", text: "Examples from its tests:" }),
        samples.map((c) => h("button", { class: "chip", type: "button", onclick: () => { task.value = c.input; } }, c.name))) : null,
      h("div", { class: "grid-2" },
        h("label", { class: "radio-card", for: "mode-sandbox" }, modeSandbox, h("b", { text: `Sandbox · version ${version}` }), h("span"),
          h("span", { class: "small muted", text: "Runs on a copy of your data. External APIs are simulated, actions only change the copy, nothing leaves the platform. Safe for any version." })),
        h("label", { class: `radio-card${deployed ? "" : " disabled"}`, for: "mode-live" }, modeLive, h("b", { text: deployed ? `Live · deployed version ${deployed}` : "Live · not deployed" }), h("span"),
          h("span", { class: "small muted", text: "Runs on your real data. Actions that need approval wait in Approvals; messages go to the outbox (no delivery connector is installed)." }))),
      h("div", { class: "row" }, runBtn)]),
    result,
  ];
}

async function tabDeploy({ id, base, ver }) {
  const admin = isAdmin();
  const note = h("input", { type: "text", id: "approval-note", placeholder: "Optional note, e.g. what you checked" });
  const deployed = base.agent.deployed_version;
  const versionPayloads = {};
  await Promise.all(base.versions.slice(0, 10).map(async (v) => { versionPayloads[v.version] = await api("GET", `/agents/${id}/versions/${v.version}`); }));
  const event = h("input", { type: "text", id: "event-name", value: ver.spec.triggers.find((t) => t.kind === "event")?.event || "" });
  const payload = h("textarea", { id: "event-payload", rows: 3 });
  const eventTriggers = (versionPayloads[deployed]?.spec.triggers || []).filter((t) => t.kind === "event");
  return [
    notice("info", h("b", { text: "Local deployment" }),
      "Deployed agents run on this server: live runs, scheduled triggers, events and follow-ups use the deployed version, " +
      "processed by the built-in worker. There is no separate production hosting yet."),
    admin ? null : notice("warn", "Only administrators can approve and deploy versions."),
    card("Versions", "A version can be approved after its latest test run passed, including every safety and permission case. " +
      "Agents that can change records, act on external systems, send messages or move money need a recorded approval before deployment.", [
      field("Approval note", note),
      table([
        { label: "Version", render: (v) => h("a", { href: `#/agents/${id}/overview${v.version !== base.version ? `/v${v.version}` : ""}`, text: `v${v.version}` }) },
        { label: "Status", render: (v) => statusBadge(v.status) },
        { label: "Tests", render: (v) => v.pass_rate === null ? "not run" : fmt.pct(v.pass_rate) },
        { label: "Approval", render: (v) => v.approved_by ? `${v.approved_by}, ${fmt.date(v.approved_at)}` : (versionPayloads[v.version]?.consequential === false ? "not needed" : "—") },
        { label: "Deployed", render: (v) => v.version === deployed ? badge("live", "ok") : "" },
        { label: "Created", render: (v) => fmt.ago(v.created_at) },
        { label: "", render: (v) => {
          const p = versionPayloads[v.version];
          if (!admin || !p) return "";
          return h("div", { class: "row" },
            p.can_approve ? actionButton("Approve", async () => { await api("POST", `/agents/${id}/versions/${v.version}/approve`, { note: note.value }); toast(`Version ${v.version} approved`); render(); }, { small: true }) : null,
            p.can_deploy ? actionButton(deployed && v.version < deployed ? "Roll back to this" : "Deploy", async () => { await api("POST", `/agents/${id}/deploy`, { version: v.version }); toast(`Version ${v.version} deployed`); render(); }, { small: true, kind: "primary" }) : null,
            v.version === deployed ? actionButton("Undeploy", async () => { await api("POST", `/agents/${id}/undeploy`); toast("Agent undeployed"); render(); }, { small: true, kind: "danger", confirmText: "Confirm undeploy" }) : null);
        } },
      ], base.versions)]),
    deployed && eventTriggers.length ? card("Send an event", "Event triggers start the deployed agent through the job queue, the same path an inbound webhook would use.", [
      h("div", { class: "form-grid" }, field("Event", event, `defined: ${eventTriggers.map((t) => t.event).join(", ")}`), h("div", { class: "full" }, field("Payload", payload))),
      h("div", { class: "row" }, actionButton("Send event", async () => {
        const r = await api("POST", `/agents/${id}/events`, { event: event.value, payload: payload.value });
        toast(`Queued job ${r.jobs.join(", ")}. The worker picks it up shortly.`);
        render();
      }))]) : null,
    card("Recent jobs", "Scheduled, event and follow-up work for this agent.", [table([
      { label: "Job", render: (j) => `#${j.id}` }, { label: "Source", render: (j) => sourceBadge(j.source) },
      { label: "Status", render: (j) => statusBadge(j.status) }, { label: "Due", render: (j) => fmt.date(j.due_at) },
      { label: "Attempts", key: "attempts" },
      { label: "Run", render: (j) => j.run_id ? h("a", { href: `#/runs/${j.run_id}`, text: `#${j.run_id}` }) : "—" },
      { label: "Error", class: "wrap", key: "error" },
    ], base.jobs, { empty: "No jobs yet." })]),
  ];
}

async function tabActivity({ id }) {
  const [m, runs] = await Promise.all([api("GET", `/agents/${id}/metrics`), api("GET", `/runs?agent_id=${id}`)]);
  return [
    metricsStats(m),
    card("Runs", null, [runsTable(runs, { showAgent: false })]),
  ];
}

function metricsStats(m) {
  return h("div", { class: "stats" },
    stat("Runs (30 days)", m.runs), stat("Completed", fmt.pct(m.completion_rate)),
    stat("Failed runs", m.failed_runs, m.failed_runs > 0),
    stat("Escalated to people", m.escalations), stat("Tool error rate", fmt.pct(m.tool_error_rate), (m.tool_error_rate || 0) > 0.2),
    stat("Approvals waiting", m.pending_actions, m.pending_actions > 0), stat("Avg duration", fmt.ms(m.avg_duration_ms)),
    stat("Model cost", fmt.usd(m.cost_usd)));
}

// ---------------------------------------------------------------- testing & deployed overviews

async function pageTesting() {
  const agents = await api("GET", "/agents");
  return [
    pageHead("Testing", "Latest test results for each agent's newest version. Open an agent's Tests tab to run or inspect them."),
    table([
      { label: "Agent", render: (a) => h("a", { href: `#/agents/${a.id}/tests`, text: a.name }) },
      { label: "Version", render: (a) => `v${a.latest_version}` },
      { label: "Status", render: (a) => statusBadge(a.latest_status) },
      { label: "Passed", render: (a) => a.latest_test ? `${a.latest_test.passed_count} of ${a.latest_test.total}` : "not run" },
      { label: "Pass rate", render: (a) => a.latest_test ? progress(a.latest_test.pass_rate, a.latest_test.passed ? "" : "bad") : "—" },
      { label: "Blockers", class: "wrap", render: (a) => a.latest_test?.blockers.join("; ") || "—" },
    ], agents, { empty: "No agents yet.", onRow: (a) => go(`#/agents/${a.id}/tests`) }),
  ];
}

async function pageDeployed() {
  const agents = (await api("GET", "/agents")).filter((a) => a.deployed_version);
  return [
    pageHead("Deployed agents", "Agents with a live version on this server (local deployment)."),
    table([
      { label: "Agent", class: "wrap", render: (a) => h("div", {}, h("a", { href: `#/agents/${a.id}/deploy`, text: a.name }), h("div", { class: "small muted", text: a.purpose })) },
      { label: "Live version", render: (a) => badge(`v${a.deployed_version}`, "ok") },
      { label: "Newer version", render: (a) => a.latest_version > a.deployed_version ? h("span", {}, `v${a.latest_version} `, statusBadge(a.latest_status)) : "—" },
      { label: "Triggers", render: (a) => a.triggers.join(", ") },
      { label: "Last run", render: (a) => fmt.ago(a.last_run_at) },
      { label: "", render: (a) => h("a", { href: `#/agents/${a.id}/run`, text: "Run a task" }) },
    ], agents, { empty: "Nothing is deployed yet. Test and approve a version, then deploy it from the agent's Versions & deployment tab." }),
  ];
}

// ---------------------------------------------------------------- runs

async function pageRuns() {
  const [runs, agents] = await Promise.all([api("GET", "/runs"), api("GET", "/agents")]);
  const fAgent = h("select", { id: "f-agent" }, h("option", { value: "", text: "All agents" }), agents.map((a) => h("option", { value: a.id, text: a.name })));
  const fSource = h("select", { id: "f-source" }, [["", "All sources"], ["live", "Live (not sandbox)"], ["sandbox", "Sandbox"], ["manual", "Manual live"]].map(([v, t]) => h("option", { value: v, text: t })));
  const fStatus = h("select", { id: "f-status" }, [["", "All statuses"], ["completed", "Completed"], ["failed", "Failed"], ["running", "Running"]].map(([v, t]) => h("option", { value: v, text: t })));
  const box = h("div", {}, runsTable(runs));
  const apply = async () => {
    const q = new URLSearchParams();
    if (fAgent.value) q.set("agent_id", fAgent.value);
    if (fSource.value) q.set("source", fSource.value);
    if (fStatus.value) q.set("status", fStatus.value);
    box.replaceChildren(runsTable(await api("GET", `/runs?${q}`)));
  };
  [fAgent, fSource, fStatus].forEach((el) => el.addEventListener("change", () => apply().catch((e) => toast(e.message, "bad"))));
  return [
    pageHead("Execution history", "Every agent run: who triggered it, what it did, how long it took and what it cost."),
    h("div", { class: "row" }, fAgent, fSource, fStatus),
    box,
  ];
}

async function pageRun({ params: [id] }) {
  const run = await api("GET", `/runs/${id}`);
  return [
    pageHead(`Run #${run.id}`, null, [statusBadge(run.status), sourceBadge(run.source)], h("a", { href: "#/runs", text: "Execution history" })),
    runDetail(run),
  ];
}

function runDetail(run, { compact = false } = {}) {
  const steps = run.events.filter((e) => !["run_started"].includes(e.event));
  return h("div", { class: "stack" },
    run.sandbox ? notice("info", "Sandbox run: it used a copy of the data. External APIs were simulated and no real action was taken.") : null,
    run.error ? notice("bad", h("b", { text: "Error" }), run.error) : null,
    card(compact ? h("h2", {}, "Run ", h("a", { href: `#/runs/${run.id}`, text: `#${run.id}` })) : "Summary", null, [kv([
      ["Agent", h("a", { href: `#/agents/${run.agent_id}`, text: run.agent })], ["Version", `v${run.version}`],
      ["Started", fmt.date(run.created_at)], ["Duration", fmt.ms(run.duration_ms)],
      ["Outcome", run.stop_reason || "running"], ["Tokens", `${run.input_tokens.toLocaleString()} in · ${run.output_tokens.toLocaleString()} out`],
      ["Model cost", fmt.usd(run.cost_usd)]])]),
    card("Task", null, [h("pre", { text: run.input })]),
    card("What the agent did", `${steps.filter((e) => e.event.startsWith("tool")).length} tool calls`, [h("div", { class: "timeline" },
      steps.length ? steps.map(eventView) : h("p", { class: "muted", text: "No steps recorded." }))]),
    run.output ? card("Final answer", null, [h("pre", { text: run.output })]) : null,
    run.actions.length ? card("Actions from this run that need approval", null, [actionsTable(run.actions)]) : null);
}

function eventView(e) {
  const d = e.data;
  if (e.event === "tool_call" || e.event === "tool_error") {
    return h("div", { class: `event${e.event === "tool_error" ? " err" : ""}` },
      h("div", { class: "head" }, h("div", { class: "row" }, h("b", { text: d.tool }),
        e.event === "tool_error" ? badge("error", "bad") : badge("ok", "ok"),
        d.simulated ? badge("simulated", "info") : null,
        d.approval && d.approval !== "not_required" ? badge(d.approval, d.approval === "approved" ? "ok" : "warn") : null),
        h("span", { class: "small muted", text: fmt.ms(d.duration_ms) })),
      h("pre", { text: typeof d.input === "object" ? fmt.json(d.input) : String(d.input) }),
      h("pre", { text: `→ ${d.result}` }));
  }
  if (e.event === "approval") {
    return h("div", { class: `event${d.decision === "queued" ? " wait" : ""}` }, h("div", { class: "head" },
      h("div", { class: "row" }, h("b", { text: `Approval for ${d.tool}` }), statusBadge(d.decision)),
      h("span", { class: "small muted", text: fmt.date(e.created_at) })), h("pre", { text: fmt.json(d.input) }));
  }
  if (e.event === "tool_blocked") {
    return h("div", { class: "event err" }, h("b", { text: `Blocked: ${d.tool} is not one of this agent's tools` }));
  }
  if (e.event === "run_failed") return h("div", { class: "event err" }, h("b", { text: "Run failed" }), h("pre", { text: d.error }));
  if (e.event === "run_finished") return h("div", { class: "event" }, h("b", { text: `Finished: ${d.stop_reason}` }));
  return h("div", { class: "event" }, h("b", { text: e.event.replaceAll("_", " ") }), h("pre", { text: fmt.json(d) }));
}

// ---------------------------------------------------------------- approvals

function actionsTable(actions) {
  return table([
    { label: "Action", render: (a) => `#${a.id}` },
    { label: "Agent", key: "agent" },
    { label: "Tool", render: (a) => h("b", { text: a.tool }) },
    { label: "Details", class: "wrap", render: (a) => h("pre", { text: fmt.json(JSON.parse(a.input_json)) }) },
    { label: "Run", render: (a) => a.run_id ? h("a", { href: `#/runs/${a.run_id}`, text: `#${a.run_id}` }) : "—" },
    { label: "Requested", render: (a) => fmt.ago(a.created_at) },
    { label: "", render: (a) => a.status !== "pending" ? h("span", {}, statusBadge(a.status), a.decided_by ? ` by ${a.decided_by}` : "")
      : isAdmin() ? h("div", { class: "row" },
        actionButton("Approve", async () => { const r = await api("POST", `/approvals/actions/${a.id}`, { decision: "approve" }); toast(`Done: ${r.result}`); render(); }, { small: true, kind: "primary" }),
        actionButton("Deny", async () => { await api("POST", `/approvals/actions/${a.id}`, { decision: "deny" }); toast("Denied"); render(); }, { small: true, kind: "danger" }))
        : badge("admin approval needed", "warn") },
  ], actions, { empty: "Nothing is waiting for approval." });
}

async function pageApprovals() {
  const a = await api("GET", "/approvals");
  return [
    pageHead("Approvals", "Consequential actions requested by deployed agents, and tested versions waiting for sign-off. " +
      "Approving an action carries it out immediately."),
    card("Actions waiting", null, [actionsTable(a.actions)]),
    card("Versions waiting for approval", "Passed all tests, including safety and permission cases.", [table([
      { label: "Agent", render: (v) => h("a", { href: `#/agents/${v.agent_id}/deploy`, text: v.agent }) },
      { label: "Version", render: (v) => `v${v.version}` }, { label: "Tests", render: (v) => fmt.pct(v.pass_rate) },
      { label: "Created", render: (v) => fmt.ago(v.created_at) },
    ], a.versions, { empty: "No versions waiting." })]),
    card("Recently decided", null, [actionsTable(a.decided)]),
  ];
}

// ---------------------------------------------------------------- monitoring

async function pageMonitoring() {
  const m = await api("GET", "/monitoring?days=30");
  const share = m.monthly_budget_usd ? m.month_spend_usd / m.monthly_budget_usd : 0;
  return [
    pageHead("Monitoring", "Last 30 days, from recorded runs and model calls."),
    card("Model spend this month", "Every model call is metered: requirements analysis, generation, tests and agent runs. New calls stop when the budget is used up.", [
      h("div", { class: "bar-label" }, h("span", { text: `${fmt.usd(m.month_spend_usd)} spent` }), h("span", { text: `budget ${fmt.usd(m.monthly_budget_usd)}` })),
      progress(share, share >= 1 ? "bad" : share >= 0.8 ? "warn" : ""),
      table([{ label: "Purpose", key: "purpose" }, { label: "Calls", key: "calls" },
        { label: "Input tokens", render: (u) => fmt.tokens(u.input_tokens) }, { label: "Output tokens", render: (u) => fmt.tokens(u.output_tokens) },
        { label: "Cost", render: (u) => fmt.usd(u.usd) }], m.usage, { empty: "No model calls in this period." })]),
    card("Agents", null, [table([
      { label: "Agent", class: "nowrap", render: (a) => h("a", { href: `#/agents/${a.id}/activity`, text: a.agent }) },
      { label: "Deployed", render: (a) => a.deployed_version ? `v${a.deployed_version}` : "—" },
      { label: "Runs", key: "runs" }, { label: "Completed", render: (a) => fmt.pct(a.completion_rate) },
      { label: "Failed", key: "failed_runs" }, { label: "Escalations", key: "escalations" },
      { label: "Tool errors", render: (a) => fmt.pct(a.tool_error_rate) },
      { label: "Approvals", render: (a) => Object.entries(a.approvals).map(([k, v]) => `${k} ${v}`).join(", ") || "—" },
      { label: "Waiting", key: "pending_actions" }, { label: "Avg duration", render: (a) => fmt.ms(a.avg_duration_ms) },
      { label: "Cost", render: (a) => fmt.usd(a.cost_usd) },
    ], m.agents, { empty: "No agents yet." })]),
    card("Failed background jobs", null, [table([
      { label: "Job", render: (j) => `#${j.id}` }, { label: "Source", render: (j) => sourceBadge(j.source) },
      { label: "Attempts", key: "attempts" }, { label: "Error", class: "wrap", key: "error" }, { label: "Created", render: (j) => fmt.ago(j.created_at) },
    ], m.failed_jobs, { empty: "No failed jobs." })]),
  ];
}

// ---------------------------------------------------------------- data & integrations

async function pageData() {
  const [data, integ] = await Promise.all([api("GET", "/data"), api("GET", "/integrations")]);
  const dsName = h("input", { type: "text", id: "ds-name", placeholder: "e.g. orders", pattern: "[a-z][a-z0-9_]*" });
  const dsFile = h("input", { type: "file", id: "ds-file", accept: ".csv,text/csv" });
  dsFile.addEventListener("change", () => {
    if (!dsName.value && dsFile.files[0]) dsName.value = dsFile.files[0].name.replace(/\.[^.]+$/, "").toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^[^a-z]+/, "");
  });
  const docTitle = h("input", { type: "text", id: "doc-title" });
  const docFile = h("input", { type: "file", id: "doc-file", accept: ".txt,.md,text/plain,text/markdown" });
  const docText = h("textarea", { id: "doc-text", rows: 4, placeholder: "…or paste the text here" });
  const preview = h("div");
  return [
    pageHead("Data & integrations", "What your agents can work with. Data stays in your organisation's own storage; agents get read-only access to the datasets their configuration names."),
    card("Datasets", "Tabular data agents can query with read-only SQL. Upload CSV exports from your systems (up to 5 MB).", [
      h("div", { class: "form-grid" }, field("Name", dsName, "lowercase, used by agents"), field("CSV file", dsFile)),
      h("div", { class: "row" }, actionButton("Upload dataset", async () => {
        if (!dsFile.files[0]) throw new Error("Choose a CSV file.");
        const r = await api("POST", "/datasets", { name: dsName.value.trim(), csv: await dsFile.files[0].text() });
        toast(`Imported ${r.rows} rows into ${r.name}`);
        render();
      }, { kind: "primary" })),
      table([
        { label: "Dataset", render: (d) => h("b", { text: d.name }) }, { label: "Rows", key: "rows" },
        { label: "Columns", class: "wrap", render: (d) => d.columns.join(", ") },
        { label: "", render: (d) => h("div", { class: "row" },
          actionButton("Preview", async () => {
            const p = await api("GET", `/datasets/${d.name}`);
            preview.replaceChildren(card(`Preview: ${d.name}`, "First 20 rows.", [table(p.columns.map((c, i) => ({ label: c, render: (r) => String(r[i] ?? "") })), p.rows)]));
          }, { small: true }),
          isAdmin() ? actionButton("Delete", async () => { await api("DELETE", `/datasets/${d.name}`); toast("Deleted"); render(); }, { small: true, kind: "danger", confirmText: "Confirm delete" }) : null) },
      ], data.datasets, { empty: "No datasets yet." }),
      preview]),
    card("Documents", "Policies, procedures, price lists. Agents with document search can read them.", [
      h("div", { class: "form-grid" }, field("Title", docTitle), field("Text or Markdown file", docFile), h("div", { class: "full" }, docText)),
      h("div", { class: "row" }, actionButton("Add document", async () => {
        const content = docFile.files[0] ? await docFile.files[0].text() : docText.value;
        const title = docTitle.value.trim() || docFile.files[0]?.name.replace(/\.[^.]+$/, "");
        if (!title || !content.trim()) throw new Error("Give the document a title and content.");
        await api("POST", "/documents", { title, content });
        toast("Document added");
        render();
      }, { kind: "primary" })),
      table([
        { label: "Document", render: (d) => h("b", { text: d.title }) }, { label: "Size", render: (d) => `${d.size.toLocaleString()} characters` },
        { label: "Added", render: (d) => fmt.ago(d.created_at) },
        { label: "", render: (d) => isAdmin() ? actionButton("Delete", async () => { await api("DELETE", `/documents/${d.id}`); toast("Deleted"); render(); }, { small: true, kind: "danger", confirmText: "Confirm delete" }) : "" },
      ], data.documents, { empty: "No documents yet." })]),
    card("Integrations in use", "Every tool of every agent, and whether it is real or simulated. Secrets are set on the server and never shown here.", [
      kv([["Model provider", h("span", {}, integ.model.provider, " ", integ.model.configured ? badge("configured", "ok") : badge("not configured", "bad"))]]),
      table([
        { label: "Agent", render: (t) => h("a", { href: `#/agents/${t.agent_id}/config`, text: t.agent }) },
        { label: "Tool", render: (t) => h("b", { text: t.tool }) },
        { label: "Type", render: (t) => badge(t.label, t.kind === "built_in" ? "" : "info") },
        { label: "Risk", render: (t) => riskBadge(t.risk) },
        { label: "Status", class: "wrap", key: "status" },
        { label: "Secret", render: (t) => t.secret_name ? h("div", {}, t.secret_configured ? badge("set", "ok") : badge("missing", "bad"), h("div", { class: "small mono", text: t.secret_env })) : "—" },
      ], integ.tools, { empty: "No agents yet." })]),
  ];
}

// ---------------------------------------------------------------- settings

async function pageSettings() {
  const s = await api("GET", "/settings");
  const admin = isAdmin();
  const users = admin ? await api("GET", "/users") : [];
  const budget = h("input", { type: "number", id: "budget", min: 0, step: 1, value: s.monthly_budget_usd, disabled: !admin });
  const nu = { name: h("input", { type: "text", id: "nu-name" }), email: h("input", { type: "email", id: "nu-email" }),
    password: h("input", { type: "password", id: "nu-password", autocomplete: "new-password" }),
    role: h("select", { id: "nu-role" }, h("option", { value: "member", text: "Member" }), h("option", { value: "admin", text: "Administrator" })) };
  return [
    pageHead("Settings"),
    card("Model provider", null, [kv([
      ["API key", s.model_configured ? badge("configured on the server", "ok") : badge("not configured", "bad")],
      ["Default model", s.default_model],
      ["How to configure", "Set ANTHROPIC_API_KEY in the server's environment and restart. The key is never sent to the browser."]])]),
    card("Spending limit", "A monthly cap on model spend for your organisation. When it is reached, new model calls are refused until you raise it or the month ends.", [
      h("div", { class: "bar-label" }, h("span", { text: `${fmt.usd(s.month_spend_usd)} spent this month` }), h("span", { text: `budget ${fmt.usd(s.monthly_budget_usd)}` })),
      progress(s.monthly_budget_usd ? s.month_spend_usd / s.monthly_budget_usd : 0),
      h("div", { class: "row" }, field("Monthly budget (USD)", budget),
        admin ? actionButton("Save budget", async () => { await api("PUT", "/settings", { monthly_budget_usd: Number(budget.value) }); toast("Budget saved"); render(); }) : null),
      h("p", { class: "small muted", text: "Costs use list prices per million tokens; prompt caching makes real costs somewhat lower." })]),
    card("Background worker", "Runs deployed agents' schedules, events and follow-ups on this server.", [kv([
      ["Status", s.worker.enabled ? (s.worker.running ? badge("running", "ok") : badge("stopped", "warn")) : badge("disabled")],
      ["Checks every", `${s.worker.poll_seconds} s`], ["Last check", fmt.ago(s.worker.last_tick)],
      s.worker.last_error ? ["Last error", s.worker.last_error] : null])]),
    card("Secrets for external APIs", "Agents refer to secrets by name (for example CRM_TOKEN). The server reads them from environment variables scoped to your organisation, so agents can never reach other organisations' or the platform's credentials.", [
      kv([["Variable prefix", h("code", { text: `${s.secret_prefix}<NAME>` })]])]),
    admin ? card("Team", "People in your organisation. Administrators can approve, deploy and change settings.", [
      table([{ label: "Name", key: "name" }, { label: "Email", key: "email" }, { label: "Role", render: (u) => badge(u.role) }, { label: "Added", render: (u) => fmt.date(u.created_at) }], users),
      h("div", { class: "form-grid" }, field("Name", nu.name), field("Email", nu.email), field("Temporary password", nu.password, "at least 10 characters"), field("Role", nu.role)),
      h("div", { class: "row" }, actionButton("Add person", async () => {
        await api("POST", "/users", { name: nu.name.value, email: nu.email.value, password: nu.password.value, role: nu.role.value });
        toast("Added");
        render();
      }))]) : null,
  ];
}

boot();
