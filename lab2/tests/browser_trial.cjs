/* Real Chromium UI checks. Never prints bearer tokens or provider credentials.
 * NODE_PATH must point to the installed Playwright package directory.
 * Pass --start-run only when another live trial is not active.
 */
const { chromium } = require("playwright");
const fs = require("node:fs");
const path = require("node:path");

async function main() {
  const root = path.resolve(__dirname, "..");
  const output = path.join(root, ".state", "trials");
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.LAB2_BROWSER_PATH ? { executablePath: process.env.LAB2_BROWSER_PATH } : {}),
  });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 }, timezoneId: "America/Los_Angeles",
  });
  const page = await context.newPage();
  const errors = [];
  let navigations = 0;
  let historyRequests = 0;
  page.on("pageerror", error => errors.push(error.message));
  page.on("framenavigated", frame => { if (frame === page.mainFrame()) navigations += 1; });
  page.on("request", request => {
    if (request.url().endsWith("/api/tracker/runs") && request.method() === "GET") historyRequests += 1;
  });
  try {
    await page.goto("http://127.0.0.1:8010/", { waitUntil: "networkidle" });
    await page.locator("#username").fill("NYUgrader");
    await page.locator("#password").fill("Courant2026!");
    const historyLoaded = page.waitForResponse(response =>
      response.url().endsWith("/api/tracker/runs") && response.request().method() === "GET"
    );
    await page.locator('#loginForm button[type="submit"]').click();
    await page.locator("#dashboard").waitFor({ state: "visible" });
    const initialRuns = await (await historyLoaded).json();
    await page.waitForFunction(count => document.querySelector("#history").options.length === count, initialRuns.length);
    const summary = {
      login: await page.locator("#dashboard").isVisible(),
      passwordCleared: await page.locator("#password").inputValue() === "",
      history: await page.locator("#history option").allTextContents(),
      currentRunId: await page.locator("#history").inputValue(),
      runButtonDisabled: await page.locator("#runNow").isDisabled(),
    };
    summary.utcTimestamps = await page.evaluate(() => {
      const naive = parseTimestamp("2026-11-25T08:01:26");
      const aware = parseTimestamp("2026-11-25T08:01:26+00:00");
      return naive.getTime() === aware.getTime() && naive.getHours() === 0;
    });

    if (process.argv.includes("--start-run")) {
      if (summary.runButtonDisabled) throw new Error("An existing run is still active; refusing duplicate trial");
      page.once("dialog", dialog => dialog.accept());
      const started = page.waitForResponse(response =>
        response.url().endsWith("/api/tracker/run-now") && response.request().method() === "POST"
      );
      await page.locator("#k").fill("5");
      await page.locator("#runNow").click();
      const response = await started;
      if (response.status() !== 202) throw new Error(`Run start returned ${response.status()}`);
      summary.startedRunId = (await response.json()).id;
      console.log(JSON.stringify({ startedRunId: summary.startedRunId }));
      await page.waitForFunction(() => document.querySelector("#runNow").disabled);
      await page.waitForFunction(id => document.querySelector("#history").value === id, summary.startedRunId);
    }

    await page.evaluate(() => {
      for (let i = 0; i < 5; i += 1) document.querySelector("#refresh").click();
    });
    await page.waitForTimeout(350);
    const navigationBaseline = navigations;
    const requestBaseline = historyRequests;
    await page.waitForTimeout(6500);
    summary.pollingWithoutNavigation = navigations === navigationBaseline;
    summary.pollRequestsAfterRapidRefresh = historyRequests - requestBaseline;
    summary.singlePollingLoop = summary.pollRequestsAfterRapidRefresh <= 4;
    const latestId = await page.locator("#history").inputValue();
    summary.displayedRunId = latestId;
    const savedRun = latestId ? await page.evaluate(id => api(`/api/tracker/runs/${id}`), latestId) : null;
    await page.screenshot({ path: path.join(output, "dashboard.png"), fullPage: true });

    // Exercise real DOM rendering with a synthetic snapshot, without persisting it.
    summary.safeRendering = await page.evaluate(() => {
      window.__injectionExecuted = false;
      render({ started_at: new Date().toISOString(), status: "complete", duration_seconds: 2,
        snapshot: { message: "UI safety fixture (not saved)", failed_workers: [], developments: [{
          rank: 1, title: '<img src=x onerror="window.__injectionExecuted=true">',
          summary: "<script>window.__injectionExecuted=true</script>", bucket: "new", stale: false,
          claims: [{ claim_id: "fixture", text: "<b>literal claim</b>", source_url: "javascript:alert(1)",
            verdict: "supported", evidence: "<img src=x onerror=alert(1)>" }],
        }], dropped: [] } });
      return !window.__injectionExecuted && document.querySelectorAll("#findings img, #findings script").length === 0
        && document.querySelector("#findings").textContent.includes("<b>literal claim</b>")
        && document.querySelectorAll("#findings a").length === 0;
    });
    if (savedRun) await page.evaluate(run => render(run), savedRun);
    else await page.evaluate(() => render({started_at: new Date().toISOString(), status: "No runs", duration_seconds: null, snapshot: {}}));

    await page.setViewportSize({ width: 390, height: 844 });
    summary.mobileNoHorizontalOverflow = await page.evaluate(() =>
      document.documentElement.scrollWidth <= window.innerWidth + 1
    );
    await page.screenshot({ path: path.join(output, "dashboard-mobile.png"), fullPage: true });
    if ((await page.locator("#history option").count()) > 1) {
      await page.locator("#history").selectOption({ index: 1 });
      await page.waitForTimeout(600);
      summary.historySelection = await page.locator("#runMeta").textContent();
    }
    await page.locator("#logout").click();
    summary.logoutClearsToken = await page.evaluate(() => !localStorage.getItem("fnms_lab2_token"));
    summary.logoutShowsLogin = await page.locator("#loginPanel").isVisible();
    summary.javascriptErrors = errors;
    fs.writeFileSync(path.join(output, "browser-trial.json"), JSON.stringify(summary, null, 2));
    console.log(JSON.stringify(summary, null, 2));
    if (!summary.login || !summary.utcTimestamps || !summary.pollingWithoutNavigation || !summary.singlePollingLoop || !summary.safeRendering ||
        !summary.mobileNoHorizontalOverflow || !summary.logoutClearsToken || errors.length) {
      throw new Error("One or more browser checks failed; inspect browser-trial.json");
    }
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
