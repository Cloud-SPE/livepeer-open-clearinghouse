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

export const getBalance = () => api("/accounts/me/balance");

export const getLedger = (limit = 20) =>
  api(`/accounts/me/ledger?limit=${limit}`);

export const listOAuthProviders = () => api("/auth/oauth/providers");

export const oauthLoginUrl = (provider) => `/v1/auth/oauth/${provider}/login`;

export const requestPasswordReset = (email) =>
  api("/auth/password-reset/request", { method: "POST", body: { email } });

export const confirmPasswordReset = (token, new_password) =>
  api("/auth/password-reset/confirm", {
    method: "POST",
    body: { token, new_password },
  });
