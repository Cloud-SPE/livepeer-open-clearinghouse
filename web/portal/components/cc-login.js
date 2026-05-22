import { LitElement, html } from "lit";
import * as api from "/portal/lib/api.js";

export class CcLogin extends LitElement {
  static properties = {
    _loading: { state: true },
    _error: { state: true },
    // When login fails with 403 email_not_verified, remember the email so
    // we can offer the user a one-click resend without re-typing it.
    _unverifiedEmail: { state: true },
    _resending: { state: true },
    _resent: { state: true },
  };

  constructor() {
    super();
    this._loading = false;
    this._error = null;
    this._unverifiedEmail = null;
    this._resending = false;
    this._resent = false;
  }

  createRenderRoot() {
    return this;
  }

  async _submit(ev) {
    ev.preventDefault();
    this._loading = true;
    this._error = null;
    this._unverifiedEmail = null;
    this._resent = false;
    const data = new FormData(ev.currentTarget);
    const email = data.get("email");
    try {
      const res = await api.login(email, data.get("password"));
      window.dispatchEvent(
        new CustomEvent("cc-login", { detail: { user: res.user } }),
      );
    } catch (err) {
      // Backend returns 403 with detail "email_not_verified" when login
      // refuses because the email isn't verified yet. Catch that exact
      // shape and offer a one-click resend instead of dumping the raw
      // error code at the user.
      const code = err.payload?.detail || err.payload?.error?.code;
      if (err.status === 403 && code === "email_not_verified") {
        this._unverifiedEmail = email;
      } else {
        this._error = err.message;
      }
    } finally {
      this._loading = false;
    }
  }

  async _resend() {
    if (!this._unverifiedEmail) return;
    this._resending = true;
    try {
      await api.resendVerification(this._unverifiedEmail);
      this._resent = true;
    } catch (err) {
      this._error = err.message;
    } finally {
      this._resending = false;
    }
  }

  _renderUnverifiedCard() {
    if (this._resent) {
      return html`
        <div class="card">
          <h2>Check your inbox</h2>
          <p>
            If <strong>${this._unverifiedEmail}</strong> is registered, we just
            sent a fresh verification link. Click it to activate your account,
            then come back here to log in.
          </p>
          <p class="mt-2 muted">
            Didn't get it? Your operator can re-send the email or approve
            the account manually — contact them.
          </p>
          <p class="mt-2"><a href="#/login">Back to log in →</a></p>
        </div>
      `;
    }
    return html`
      <div class="card">
        <h2>Verify your email first</h2>
        <p>
          Your account exists but the email
          <strong>${this._unverifiedEmail}</strong> hasn't been verified yet.
          We sent the link when you signed up — check your inbox (and spam
          folder).
        </p>
        <p class="mt-1 muted">
          Need another copy?
        </p>
        <div class="row mt-1">
          <button
            class="primary"
            ?disabled=${this._resending}
            @click=${this._resend}
          >
            ${this._resending ? "Sending…" : "Resend verification email"}
          </button>
          <button
            class="ghost"
            ?disabled=${this._resending}
            @click=${() => {
              this._unverifiedEmail = null;
            }}
          >
            Back
          </button>
        </div>
        ${this._error ? html`<div class="msg error mt-2">${this._error}</div>` : null}
      </div>
    `;
  }

  render() {
    if (this._unverifiedEmail) return this._renderUnverifiedCard();
    return html`
      <div class="card">
        <h2>Log in</h2>
        <cc-oauth-buttons></cc-oauth-buttons>
        <form class="form" @submit=${this._submit}>
          <div class="field">
            <label for="email">Email</label>
            <input id="email" name="email" type="email" required autocomplete="email" />
          </div>
          <div class="field">
            <label for="password">Password</label>
            <input
              id="password"
              name="password"
              type="password"
              required
              autocomplete="current-password"
            />
          </div>
          ${this._error ? html`<div class="msg error">${this._error}</div>` : null}
          <button class="primary" type="submit" ?disabled=${this._loading}>
            ${this._loading ? "Signing in…" : "Log in"}
          </button>
        </form>
        <p class="mt-2 muted">
          No account? <a href="#/signup">Sign up</a>.
          · <a href="#/forgot-password">Forgot password?</a>
        </p>
      </div>
    `;
  }
}
customElements.define("cc-login", CcLogin);
