import { LitElement, html } from "lit";
import * as api from "/portal/lib/api.js";

export class CcForgotPassword extends LitElement {
  static properties = {
    _loading: { state: true },
    _sent: { state: true },
    _error: { state: true },
  };

  constructor() {
    super();
    this._loading = false;
    this._sent = false;
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
      await api.requestPasswordReset(data.get("email"));
      this._sent = true;
    } catch (err) {
      this._error = err.message;
    } finally {
      this._loading = false;
    }
  }

  render() {
    if (this._sent) {
      return html`
        <div class="card">
          <h2>Check your inbox</h2>
          <p>
            If an account exists for that email, we just sent a password
            reset link. The link expires in 60 minutes.
          </p>
          <p class="mt-2"><a href="#/login">Back to log in →</a></p>
        </div>
      `;
    }
    return html`
      <div class="card">
        <h2>Reset your password</h2>
        <p class="muted">
          Enter the email you signed up with. We'll send you a one-time
          link to set a new password.
        </p>
        <form class="form mt-2" @submit=${this._submit}>
          <div class="field">
            <label for="email">Email</label>
            <input id="email" name="email" type="email" required autocomplete="email" />
          </div>
          ${this._error ? html`<div class="msg error">${this._error}</div>` : null}
          <button class="primary" type="submit" ?disabled=${this._loading}>
            ${this._loading ? "Sending…" : "Send reset link"}
          </button>
        </form>
        <p class="mt-2 muted">
          Remembered it? <a href="#/login">Log in</a>.
        </p>
      </div>
    `;
  }
}
customElements.define("cc-forgot-password", CcForgotPassword);
