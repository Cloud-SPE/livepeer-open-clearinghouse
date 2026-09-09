// Admin SPA API client.
//
// Auth: the operator pastes the bootstrap token at the login screen; we
// stash it in localStorage and attach it as `Authorization: Bearer ...` to
// every call. The portal/user surface uses a session cookie; the admin
// surface uses a bearer token.

const BASE = "/v1/admin";
const TOKEN_KEY = "livepeer_open_clearinghouse:admin:token";

export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
  window.dispatchEvent(new CustomEvent("cc-admin-auth", { detail: { token } }));
}

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

export function clearToken() {
  setToken(null);
}

async function api(path, { method = "GET", body } = {}) {
  const token = getToken();
  const res = await fetch(BASE + path, {
    method,
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: body == null ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = { error: { message: text } };
  }
  if (!res.ok) {
    const message =
      (payload && payload.error && (payload.error.message || payload.error.code)) ||
      (payload && payload.detail) ||
      `HTTP ${res.status}`;
    const err = new Error(message);
    err.status = res.status;
    // Keep the structured envelope: callers such as the resolve dialog turn
    // `details.reason` into plain language instead of echoing the message.
    err.code = payload?.error?.code ?? null;
    err.details = payload?.error?.details ?? null;
    throw err;
  }
  return payload;
}

export const listPending = () => api("/users/pending");
export const approveUser = (id) =>
  api(`/users/${id}/approve`, { method: "POST" });
export const listUsers = (limit = 100, offset = 0) =>
  api(`/users?limit=${limit}&offset=${offset}`);
export const topupUser = (id, amount_wei, kind = "manual") =>
  api(`/users/${id}/topup`, {
    method: "POST",
    body: { amount_wei, kind },
  });
export const getBillingConfig = (id) => api(`/users/${id}/billing-config`);
export const putBillingConfig = (id, body) =>
  api(`/users/${id}/billing-config`, { method: "PUT", body });
export const listAudit = (limit = 100) => api(`/audit?limit=${limit}`);
export const listDepositSnapshots = (limit = 100) =>
  api(`/deposit-snapshots?limit=${limit}`);
export const resendVerification = (id) =>
  api(`/users/${id}/resend-verification`, { method: "POST" });
export const listCapabilities = () => api("/discovery/capabilities");
export const listOrchestrators = (capability) => {
  const qs = capability ? `?capability=${encodeURIComponent(capability)}` : "";
  return api(`/discovery/orchestrators${qs}`);
};

export const listOperators = () => api("/operators");
export const createOperator = ({ email, name, role }) =>
  api("/operators", { method: "POST", body: { email, name, role } });
export const updateOperator = (id, body) =>
  api(`/operators/${id}`, { method: "PATCH", body });
export const revokeOperator = (id) =>
  api(`/operators/${id}/revoke`, { method: "POST" });
export const rotateOperatorToken = (id) =>
  api(`/operators/${id}/rotate-token`, { method: "POST" });

// --- Usage visibility -------------------------------------------------------
//
// All wei figures in these payloads are integer strings; feed them to
// lib/format.js (BigInt) rather than Number.

function qs(params) {
  const parts = [];
  for (const [k, v] of Object.entries(params || {})) {
    if (v == null || v === "") continue;
    parts.push(`${encodeURIComponent(k)}=${encodeURIComponent(v)}`);
  }
  return parts.length ? `?${parts.join("&")}` : "";
}

export const getUserUsageOverview = (id) => api(`/users/${id}/usage/overview`);
export const listUserUsageJobs = (id, params = {}) =>
  api(`/users/${id}/usage/jobs${qs(params)}`);
export const getUserUsageSummary = (id, params = {}) =>
  api(`/users/${id}/usage/summary${qs(params)}`);
export const getFleetUsageSummary = (params = {}) =>
  api(`/usage/summary${qs(params)}`);
export const listFleetUsageJobs = (params = {}) =>
  api(`/usage/jobs${qs(params)}`);
export const getUsageAttention = (params = {}) =>
  api(`/usage/attention${qs(params)}`);
export const getWholesaleOverview = () => api("/wholesale");

// --- Settlement recourse ----------------------------------------------------
//
// POST /v1/admin/jobs/{id}/resolve closes a job the reconciler could not
// settle. `action` is one of refund_hold | accept_reported | charge_full.
// A 409 carries `details.reason` (already_closed | no_broker_report |
// not_found) on the thrown error.
export const resolveJob = (jobId, action, note = null) =>
  api(`/jobs/${encodeURIComponent(jobId)}/resolve`, {
    method: "POST",
    body: { action, note: note && note.trim() ? note.trim() : null },
  });
