// Admin SPA API client.
//
// Auth: the operator pastes the bootstrap token at the login screen; we
// stash it in localStorage and attach it as `Authorization: Bearer ...` to
// every call. The portal/user surface uses a session cookie; the admin
// surface uses a bearer token.

const BASE = "/v1/admin";
const TOKEN_KEY = "pymthouse:admin:token";

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
