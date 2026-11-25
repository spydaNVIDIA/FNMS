"use strict";
const TOKEN_KEY = "fnms_lab2_token";
let pollTimer = null;
let selectionVersion = 0;
let refreshVersion = 0;
const byId = id => document.getElementById(id);
function node(tag, text, className) {
  const value = document.createElement(tag);
  if (text !== undefined) value.textContent = String(text);
  if (className) value.className = className;
  return value;
}
function parseTimestamp(value) {
  // SQLite can omit the offset from stored UTC datetimes. Keep their UTC meaning
  // before converting to the browser's local time for display.
  const hasOffset = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value);
  return new Date(hasOffset ? value : `${value}Z`);
}
async function api(path, options = {}) {
  const token = localStorage.getItem(TOKEN_KEY);
  const response = await fetch(path, {...options, headers: {
    "Content-Type": "application/json", ...(token ? {Authorization: `Bearer ${token}`} : {})
  }});
  const body = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/auth/login") logout();
    throw new Error(typeof body.detail === "string" ? body.detail : `Request failed (${response.status})`);
  }
  return body;
}
function fail(error) { byId("error").textContent = error.message; }
function logout() {
  localStorage.removeItem(TOKEN_KEY);
  if (pollTimer) clearTimeout(pollTimer);
  refreshVersion += 1; selectionVersion += 1;
  byId("loginPanel").hidden = false; byId("dashboard").hidden = true; byId("logout").hidden = true;
}
async function login() {
  const body = {username: byId("username").value.trim(), password: byId("password").value};
  const result = await api("/api/auth/login", {method: "POST", body: JSON.stringify(body)});
  localStorage.setItem(TOKEN_KEY, result.token); byId("password").value = "";
  await initialize();
}
byId("loginForm").onsubmit = async event => {event.preventDefault(); try {await login();} catch(error) {fail(error);}};
byId("register").onclick = async () => {
  try {
    await api("/api/auth/register", {method: "POST", body: JSON.stringify({username: byId("username").value.trim(), password: byId("password").value})});
    await login();
  } catch(error) {fail(error);}
};
function render(run) {
  const snapshot = run.snapshot;
  byId("runMeta").textContent = `${parseTimestamp(run.started_at).toLocaleString()} · ${run.status} · ${run.duration_seconds === null ? "in progress" : Math.round(run.duration_seconds) + " seconds"}`;
  byId("failures").textContent = (snapshot.failed_workers || []).map(f => `${f.worker}: ${f.reason}`).join("; ");
  byId("runStatus").textContent = snapshot.message || "Research is running…";
  const used = snapshot.charged_usage;
  byId("usage").textContent = used ? `Charged usage: ${used.steps} model calls · ${used.fetches} page requests · ${used.tokens} tokens. Unanswered requests are charged their full allocation.` : "";
  const findings = byId("findings"); findings.replaceChildren();
  for (const item of snapshot.developments || []) {
    const card = node("article", undefined, "finding");
    card.append(node("h3", `${item.rank}. ${item.title}`), node("span", item.stale ? "stale" : item.bucket, "badge"), node("p", item.summary));
    for (const claim of item.claims) {
      const row = node("div", undefined, "claim");
      row.append(node("span", claim.verdict, `badge ${claim.verdict}`), node("span", claim.text));
      try {
        const url = new URL(claim.source_url);
        if (["http:", "https:"].includes(url.protocol)) {
          const link = node("a", claim.source_url); link.href = url.href; link.target = "_blank"; link.rel = "noopener noreferrer";
          row.append(node("br"), link);
        }
      } catch (_) { /* Invalid links remain absent; content is always plain text. */ }
      if (claim.evidence) row.append(node("blockquote", claim.evidence));
      card.append(row);
    }
    findings.append(card);
  }
  if (!findings.children.length) findings.append(node("p", "No findings available for this run."));
  byId("dropped").textContent = `Dropped: ${(snapshot.dropped || []).join(", ") || "None"}`;
}
async function selectRun(id) {
  const version = ++selectionVersion;
  const run = await api(`/api/tracker/runs/${id}`);
  if (version === selectionVersion) render(run);
}
async function refresh(preferNewest = false) {
  const version = ++refreshVersion;
  selectionVersion += 1;
  if (pollTimer) clearTimeout(pollTimer);
  const runs = await api("/api/tracker/runs");
  if (version !== refreshVersion) return;
  const history = byId("history"), previous = history.value;
  history.replaceChildren();
  for (const run of runs) {
    const option = node("option", `${parseTimestamp(run.started_at).toLocaleString()} — ${run.status}`);
    option.value = run.id; history.append(option);
  }
  if (!preferNewest && runs.some(r => r.id === previous)) history.value = previous;
  if (history.value) await selectRun(history.value);
  // Only the latest refresh may schedule another poll. Rapid manual refreshes
  // must not create parallel polling loops, or restart polling after logout.
  if (version !== refreshVersion) return;
  const running = runs.some(r => r.status === "running");
  byId("runNow").disabled = running;
  if (running) pollTimer = setTimeout(() => refresh().catch(fail), 2000);
}
async function initialize() {
  await api("/api/auth/me"); byId("error").textContent = "";
  byId("loginPanel").hidden = true; byId("dashboard").hidden = false; byId("logout").hidden = false;
  await refresh();
}
byId("runForm").onsubmit = async event => {
  event.preventDefault();
  if (!confirm("Start a live research and verification run using your configured API accounts?")) return;
  byId("runNow").disabled = true; byId("error").textContent = "";
  try {
    await api("/api/tracker/run-now", {method: "POST", body: JSON.stringify({topic: byId("topic").value, k: Number(byId("k").value)})});
    await refresh(true);
  } catch(error) {fail(error); byId("runNow").disabled = false;}
};
byId("history").onchange = () => selectRun(byId("history").value).catch(fail);
byId("refresh").onclick = () => refresh().catch(fail);
byId("logout").onclick = logout;
if (localStorage.getItem(TOKEN_KEY)) initialize().catch(fail);
