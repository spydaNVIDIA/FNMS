// Shared frontend helpers for the FNMS Lab1a app.
// The backend runs on a different origin (port 8000) than this static site
// (port 5173), so every call below is a genuine cross-origin request.

const API_BASE = "http://localhost:8000";
const TOKEN_KEY = "lab1a_token";

function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

function setToken(token) {
  localStorage.setItem(TOKEN_KEY, token);
}

function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

// Thin fetch wrapper that attaches the Bearer token and parses JSON.
async function api(path, { method = "GET", body, auth = false } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (auth) {
    const token = getToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
  }

  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });

  // 204 No Content (e.g. DELETE) has no JSON body.
  let data = null;
  if (res.status !== 204) {
    try {
      data = await res.json();
    } catch (_) {
      data = null;
    }
  }

  if (!res.ok) {
    const message = (data && data.detail) || `Request failed (${res.status})`;
    const error = new Error(message);
    error.status = res.status;
    throw error;
  }
  return data;
}

// Redirect to login if there's no token. Call at the top of protected pages.
function requireAuth() {
  if (!getToken()) {
    window.location.href = "login.html";
  }
}

function logout() {
  clearToken();
  window.location.href = "login.html";
}
