// Browser test of the full customer journey through the real UI and API.
// The server is tests/e2e_server.py (real app, scripted model). Run:
//   node tests/ui_e2e.cjs http://127.0.0.1:8765 SCREENSHOT_DIR
const path = require("path");
const { execSync } = require("child_process");
const { chromium } = require(path.join(execSync("npm root -g").toString().trim(), "playwright"));

const BASE = process.argv[2];
const SHOTS = process.argv[3];
const DATA = path.join(__dirname, "..", "examples", "sales_insights", "data");
const steps = [];
const errors = [];

function step(name) { steps.push(name); console.log("·", name); }

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1360, height: 900 } });
  page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
  // The first /api/me before signing in is an expected 401; anything else is a real error.
  let signedIn = false;
  page.on("console", (m) => { if (m.type() === "error" && !(m.text().includes("401") && !signedIn)) errors.push(`console: ${m.text()}`); });
  const run = Date.now().toString(36);
  const shot = (name) => page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: true });
  const waitText = (text, timeout = 30000) => page.getByText(text, { exact: false }).first().waitFor({ timeout });

  try {
    await page.goto(BASE);
    step("sign up a new organisation");
    await page.getByRole("button", { name: "Create an account" }).click();
    await page.fill("#auth-org", `Nordhem Design ${run}`);
    await page.fill("#auth-name", "Karin Lund");
    await page.fill("#auth-email", `karin+${run}@nordhem.se`);
    await page.fill("#auth-password", "correct horse battery");
    await page.getByRole("button", { name: "Create account" }).click();
    await page.getByRole("heading", { name: "Overview" }).waitFor();
    signedIn = true;
    await shot("01-overview-empty");

    step("upload sales data");
    await page.click('a[href="#/data"]');
    for (const name of ["orders", "products"]) {
      await page.setInputFiles("#ds-file", path.join(DATA, `${name}.csv`));
      if ((await page.inputValue("#ds-name")) !== name) throw new Error("dataset name was not prefilled");
      await page.getByRole("button", { name: "Upload dataset" }).click();
      await waitText(`into ${name}`);
      await page.locator("#ds-name").waitFor();
    }
    await page.getByRole("button", { name: "Preview" }).first().click();
    await waitText("First 20 rows");
    await shot("02-data");

    step("describe the business problem");
    await page.click('a[href="#/projects/new"]');
    await page.fill("#intake-title", "Weekly sales reporting");
    await page.fill("#intake-company", "Nordhem Design");
    await page.fill("#intake-industry", "Home goods retail");
    await page.fill("#intake-problem", "We spend too much time answering questions about our sales performance, collecting figures from different sources, and preparing weekly reports.");
    await page.fill("#intake-workflow", "Every Monday the controller exports orders and builds the report in Excel.");
    await page.fill("#intake-outcome", "A correct weekly report every Monday and fast answers to ad-hoc questions.");
    await page.fill("#intake-systems", "Webshop and POS export CSV.");
    await page.fill("#intake-constraints", "Reports go to management only.");
    await page.getByRole("button", { name: "Save and continue" }).click();
    await page.getByRole("heading", { name: "Weekly sales reporting" }).waitFor();

    step("generate and answer follow-up questions");
    await page.getByRole("button", { name: "Generate questions" }).click();
    await page.locator("#answer-0").waitFor({ timeout: 30000 });
    await page.fill("#answer-0", "The controller, about three hours every Monday.");
    await page.getByRole("button", { name: "Save answers" }).click();
    await waitText("Answers saved");

    step("analyse requirements");
    await page.getByRole("button", { name: "Analyse requirements" }).click();
    await waitText("Recommended agent");
    await page.fill("#amendments", "Revenue must exclude VAT.");
    await page.getByRole("button", { name: "Save corrections" }).click();
    await waitText("Corrections saved");
    await shot("03-project-analysed");

    step("generate the agent");
    await page.getByRole("button", { name: "Generate agent" }).click();
    await page.getByRole("link", { name: "open the agent" }).waitFor({ timeout: 30000 });
    await shot("04-project-generated");
    await page.getByRole("link", { name: "open the agent" }).click();
    await waitText("Tools and permissions");
    await waitText("always required");
    await shot("05-agent-overview");

    step("configuration enforces approval on sending");
    await page.getByRole("link", { name: "Configuration", exact: true }).click();
    if (!(await page.locator("#approve-send_report").isDisabled())) throw new Error("send_report approval should be locked on");
    if (!(await page.locator("#approve-send_report").isChecked())) throw new Error("send_report approval should be on");
    await shot("06-agent-config");

    step("run the test suite");
    await page.getByRole("link", { name: "Tests", exact: true }).click();
    await page.getByRole("button", { name: "Run test suite" }).click();
    await waitText("All gates passed", 60000);
    await waitText("data down");
    await shot("07-agent-tests");

    step("execute a safe task in the sandbox");
    await page.getByRole("link", { name: "Run a task", exact: true }).click();
    await page.getByRole("button", { name: "revenue by region" }).click();
    await page.getByRole("button", { name: "Run", exact: true }).click();
    await waitText("What the agent did", 30000);
    await waitText("Stockholm | 10325");
    await waitText("→ 20651");
    if (await page.getByText(/^null$/).count()) throw new Error('a stray "null" is rendered');
    await shot("08-agent-run-sandbox");

    step("approve and deploy");
    await page.getByRole("link", { name: "Versions & deployment", exact: true }).click();
    await page.fill("#approval-note", "Reviewed the test report");
    await page.getByRole("button", { name: "Approve" }).click();
    await waitText("Version 1 approved");
    await page.getByRole("button", { name: "Deploy" }).click();
    await waitText("deployed v1");
    await shot("09-agent-deployed");

    step("live run and event through the worker");
    await page.getByRole("link", { name: "Run a task", exact: true }).click();
    await page.check("#mode-live");
    await page.fill("#run-task", "Prepare the weekly sales report.");
    await page.getByRole("button", { name: "Run", exact: true }).click();
    await waitText("What the agent did", 30000);
    await waitText("draft_report");
    await page.getByRole("link", { name: "Versions & deployment", exact: true }).click();
    await page.fill("#event-payload", "Week 41");
    await page.getByRole("button", { name: "Send event" }).click();
    await waitText("Queued job");
    await page.waitForTimeout(5000); // worker polls every 2 s in this test server
    await page.reload();
    await waitText("Recent jobs");

    step("execution history, run details, monitoring");
    await page.click('a[href="#/runs"]');
    await page.getByRole("heading", { name: "Execution history" }).waitFor();
    const rows = await page.locator("tbody tr").count();
    if (rows < 3) throw new Error(`expected at least 3 runs (sandbox, live, event), saw ${rows}`);
    await shot("10-runs");
    await page.locator("tbody tr").first().click();
    await waitText("What the agent did");
    await shot("11-run-detail");
    await page.click('a[href="#/monitoring"]');
    await waitText("Model spend this month");
    await shot("12-monitoring");
    for (const [href, title] of [["#/approvals", "Approvals"], ["#/testing", "Testing"], ["#/deployed", "Deployed agents"],
      ["#/settings", "Settings"], ["#/", "Overview"], ["#/agents", "Agents"], ["#/projects", "Projects"]]) {
      await page.click(`a[href="${href}"]`);
      await page.getByRole("heading", { name: title, exact: true }).waitFor();
    }
    await shot("13-overview");
    await page.click('a[href="#/data"]');
    await waitText("Integrations in use");
    await shot("14-integrations");

    step("phone layout");
    await page.setViewportSize({ width: 400, height: 860 });
    await page.click('a[href="#/"]').catch(() => {});
    await page.goto(BASE + "/#/runs");
    await page.getByRole("heading", { name: "Execution history" }).waitFor();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
    if (overflow) throw new Error("page scrolls horizontally at phone width");
    await shot("15-phone-runs");
  } catch (e) {
    errors.push(`FAILED at "${steps.at(-1)}": ${e.message}`);
    await shot("failure").catch(() => {});
  }
  await browser.close();
  if (errors.length) {
    console.error("\nUI E2E FAILED\n" + errors.join("\n"));
    process.exit(1);
  }
  console.log(`\nUI E2E PASSED (${steps.length} steps)`);
})();
