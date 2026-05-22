// Lightweight fetch wrapper for the portal SPA.
//
// Authentication is via the session cookie set by /v1/auth/login; we just
// include credentials on every call.

const BASE = "/v1";

export async function api(path, { method = "GET", body, headers } = {}) {
  const res = await fetch(BASE + path, {
    method,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
      ...(headers || {}),
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
    err.payload = payload;
    throw err;
  }
  return payload;
}

export const signup = (email, password) =>
  api("/accounts/signup", { method: "POST", body: { email, password } });

export const verifyEmail = (token) =>
  api("/accounts/verify-email", { method: "POST", body: { token } });

export const login = (email, password) =>
  api("/auth/login", { method: "POST", body: { email, password } });

export const logout = () => api("/auth/logout", { method: "POST" });

export const me = () => api("/accounts/me");

export const listApiKeys = () => api("/accounts/me/api-keys");

export const createApiKey = (label) =>
  api("/accounts/me/api-keys", { method: "POST", body: { label } });

export const revokeApiKey = (id) =>
  api(`/accounts/me/api-keys/${id}`, { method: "DELETE" });
