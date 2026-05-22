import { LitElement, html } from "lit";
import * as api from "/portal/lib/api.js";

export class CcSignup extends LitElement {
  static properties = {
    _loading: { state: true },
    _success: { state: true },
    _error: { state: true },
  };

  constructor() {
    super();
    this._loading = false;
    this._success = false;
    this._error = null;
  }

  createRenderRoot() {
    return this;
  }

  async _submit(ev) {
    ev.preventDefault();
    this._loading = true;
    this._error = null;
    const data = new FormData(ev.currentTarget);
    try {
      await api.signup(data.get("email"), data.get("password"));
      this._success = true;
    } catch (err) {
      this._error = err.message;
    } finally {
      this._loading = false;
    }
  }

  render() {
    if (this._success) {
      return html`
        <div class="card">
          <h2>Check your inbox</h2>
          <p>
            We've sent a verification link to your email. Click it to activate
            your account. After verification, an operator will review your
            sign-up.
          </p>
          <p class="mt-2">
            <a href="#/login">Go to log in →</a>
          </p>
        </div>
      `;
    }
    return html`
      <div class="card">
        <h2>Create your account</h2>
        <cc-oauth-buttons></cc-oauth-buttons>
        <form class="form" @submit=${this._submit}>
          <div class="field">
            <label for="email">Email</label>
            <input id="email" name="email" type="email" required autocomplete="email" />
          </div>
          <div class="field">
            <label for="password">Password (min. 12 chars)</label>
            <input
              id="password"
              name="password"
              type="password"
              required
              minlength="12"
              autocomplete="new-password"
            />
          </div>
          ${this._error ? html`<div class="msg error">${this._error}</div>` : null}
          <button class="primary" type="submit" ?disabled=${this._loading}>
            ${this._loading ? "Creating…" : "Sign up"}
          </button>
        </form>
        <p class="mt-2 muted">
          Already have an account? <a href="#/login">Log in</a>.
        </p>
      </div>
    `;
  }
}
customElements.define("cc-signup", CcSignup);
