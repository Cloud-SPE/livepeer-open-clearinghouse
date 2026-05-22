import { LitElement, html } from "lit";
import * as api from "/portal/lib/api.js";

export class CcResetPassword extends LitElement {
  static properties = {
    token: { type: String },
    _loading: { state: true },
    _done: { state: true },
    _error: { state: true },
  };

  constructor() {
    super();
    this.token = "";
    this._loading = false;
    this._done = false;
    this._error = null;
  }

  createRenderRoot() {
    return this;
  }

  async _submit(ev) {
    ev.preventDefault();
    if (!this.token) {
      this._error = "Missing reset token.";
      return;
    }
    const data = new FormData(ev.currentTarget);
    const a = data.get("password");
    const b = data.get("confirm");
    if (a !== b) {
      this._error = "Passwords don't match.";
      return;
    }
    if (String(a).length < 12) {
      this._error = "Password must be at least 12 characters.";
      return;
    }
    this._loading = true;
    this._error = null;
    try {
      await api.confirmPasswordReset(this.token, a);
      this._done = true;
    } catch (err) {
      this._error = err.message;
    } finally {
      this._loading = false;
    }
  }

  render() {
    if (!this.token) {
      return html`
        <div class="card">
          <h2>No token</h2>
          <p>
            This page needs a reset token. Open the link from your email,
            or <a href="#/forgot-password">request a new one</a>.
          </p>
        </div>
      `;
    }
    if (this._done) {
      return html`
        <div class="card">
          <h2>Password updated</h2>
          <p>You can now log in with your new password.</p>
          <p class="mt-2"><a href="#/login">Log in →</a></p>
        </div>
      `;
    }
    return html`
      <div class="card">
        <h2>Set a new password</h2>
        <form class="form mt-2" @submit=${this._submit}>
          <div class="field">
            <label for="password">New password (min. 12 chars)</label>
            <input
              id="password"
              name="password"
              type="password"
              required
              minlength="12"
              autocomplete="new-password"
            />
          </div>
          <div class="field">
            <label for="confirm">Confirm new password</label>
            <input
              id="confirm"
              name="confirm"
              type="password"
              required
              minlength="12"
              autocomplete="new-password"
            />
          </div>
          ${this._error ? html`<div class="msg error">${this._error}</div>` : null}
          <button class="primary" type="submit" ?disabled=${this._loading}>
            ${this._loading ? "Saving…" : "Set new password"}
          </button>
        </form>
      </div>
    `;
  }
}
customElements.define("cc-reset-password", CcResetPassword);
