"use strict";

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function safeSourceLink(url) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
    const link = element("a", parsed.hostname, "text-indigo-300 hover:underline break-all");
    link.href = parsed.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    return link;
  } catch (_) {
    return null;
  }
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "In progress";
  const totalSeconds = Math.max(0, Math.round(Number(seconds)));
  if (totalSeconds < 1) return "<1s";
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const remainingSeconds = totalSeconds % 60;
  if (minutes < 60) return `${minutes}m ${remainingSeconds}s`;
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  return `${hours}h ${remainingMinutes}m ${remainingSeconds}s`;
}

function renderDevelopments(items) {
  const container = document.getElementById("developments");
  container.replaceChildren();
  if (!items.length) {
    container.append(element("p", "No ranked developments yet.", "text-slate-400"));
    return;
  }
  for (const item of items) {
    const card = element("article", null, "rounded-2xl border border-white/10 bg-white/5 p-5");
    const heading = element(
      "h3",
      `${item.rank ? `${item.rank}. ` : ""}${item.title}`,
      "text-lg font-semibold",
    );
    const badge = element(
      "span",
      item.bucket,
      "ml-3 rounded-full bg-indigo-500/20 px-2 py-1 text-xs uppercase tracking-wide text-indigo-200",
    );
    heading.append(badge);
    card.append(heading);
    card.append(element("p", item.summary, "mt-3 text-sm leading-6 text-slate-300"));
    const sourceList = element("ul", null, "mt-4 space-y-1 text-sm");
    for (const source of item.source_urls) {
      const link = safeSourceLink(source);
      if (!link) continue;
      const row = element("li");
      row.append(link);
      sourceList.append(row);
    }
    card.append(sourceList);
    container.append(card);
  }
}

async function renderArticles(runId) {
  const body = document.getElementById("articleRows");
  body.replaceChildren();
  const articles = await api(`/api/tracker/runs/${runId}/articles`, { auth: true });
  for (const article of articles) {
    const row = element("tr", null, "border-t border-white/10");
    const titleCell = element("td", null, "px-3 py-3");
    const link = safeSourceLink(article.url);
    titleCell.append(element("div", article.title, "font-medium"));
    if (link) titleCell.append(link);
    row.append(titleCell);
    row.append(element("td", new Date(article.fetched_at).toLocaleString(), "px-3 py-3 text-slate-300"));
    row.append(element("td", article.status, "px-3 py-3"));
    row.append(element("td", article.rejection_reason || "—", "px-3 py-3 text-slate-400"));
    body.append(row);
  }
  if (!articles.length) {
    const row = element("tr");
    const cell = element("td", "No articles recorded for this run.", "px-3 py-4 text-slate-400");
    cell.colSpan = 4;
    row.append(cell);
    body.append(row);
  }
}

async function renderRun(runId, isLatest) {
  const [report] = await Promise.all([
    api(`/api/tracker/runs/${runId}/report`, { auth: true }),
    renderArticles(runId),
  ]);
  document.getElementById("reportHeading").textContent =
    isLatest ? "Latest report" : "Selected run report";
  document.getElementById("runMeta").textContent =
    `${new Date(report.run.started_at).toLocaleString()} · ${report.run.status} · ` +
    `${formatDuration(report.run.duration_seconds)}`;
  renderDevelopments(report.developments);
}

async function loadTracker() {
  try {
    const me = await api("/api/auth/me", { auth: true });
    document.getElementById("who").textContent = me.username;
    const runs = await api("/api/tracker/runs", { auth: true });
    const runSelect = document.getElementById("runSelect");
    runSelect.replaceChildren();
    for (const run of runs) {
      const option = element(
        "option",
        `${new Date(run.started_at).toLocaleString()} — ${run.status} — ${formatDuration(run.duration_seconds)}`,
      );
      option.value = String(run.id);
      runSelect.append(option);
    }
    if (runs.length) {
      await renderRun(runs[0].id, true);
    } else {
      document.getElementById("reportHeading").textContent = "Latest report";
      document.getElementById("runMeta").textContent = "No runs yet";
      renderDevelopments([]);
      await renderArticles(0).catch(() => {});
    }
    runSelect.onchange = () => renderRun(runSelect.value, runSelect.selectedIndex === 0);
  } catch (error) {
    if (error.status === 401) logout();
    document.getElementById("error").textContent = error.message;
  }
}

let runStatusTimer = null;
const RUN_COMPLETION_KEY = "lab1b_handled_run_completion";

function showRunStatus(status) {
  const button = document.getElementById("runNow");
  const label = document.getElementById("runNowStatus");
  const active = status.state === "running" || status.state === "busy";
  button.disabled = active;
  button.textContent = status.state === "running" ? "Tracker is running…" : "Run tracker now";
  label.textContent = status.message;
}

async function pollRunStatus() {
  try {
    const status = await api("/api/tracker/run-now/status", { auth: true });
    showRunStatus(status);
    if (status.state === "running" || status.state === "busy") {
      runStatusTimer = window.setTimeout(pollRunStatus, 2000);
    } else if (status.state === "succeeded" && status.completed_at) {
      const completionMarker = `${status.run_id || "no-run-id"}:${status.completed_at}`;
      if (window.sessionStorage.getItem(RUN_COMPLETION_KEY) !== completionMarker) {
        window.sessionStorage.setItem(RUN_COMPLETION_KEY, completionMarker);
        document.getElementById("runNowStatus").textContent =
          `${status.message}. Updating the report…`;
        await loadTracker();
        document.getElementById("runNowStatus").textContent = status.message;
      }
    }
  } catch (error) {
    document.getElementById("runNowStatus").textContent = error.message;
    document.getElementById("runNow").disabled = false;
  }
}

async function startTrackerRun() {
  const confirmed = window.confirm(
    "Run the tracker now? This makes live Groq and Tavily API calls and saves another run. " +
      "A run less than 24 hours after Run 1 is diagnostic and will not become reports/run2.md.",
  );
  if (!confirmed) return;
  const button = document.getElementById("runNow");
  button.disabled = true;
  document.getElementById("runNowStatus").textContent = "Starting tracker…";
  try {
    const status = await api("/api/tracker/run-now", { method: "POST", auth: true });
    showRunStatus(status);
    if (runStatusTimer) window.clearTimeout(runStatusTimer);
    runStatusTimer = window.setTimeout(pollRunStatus, 1000);
  } catch (error) {
    document.getElementById("runNowStatus").textContent = error.message;
    button.disabled = false;
  }
}

document.getElementById("runNow").addEventListener("click", startTrackerRun);
loadTracker();
pollRunStatus();
