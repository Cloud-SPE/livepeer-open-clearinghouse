import { LitElement, html } from "lit";
import * as api from "/portal/lib/api.js";

export class CcVerifyEmail extends LitElement {
  static properties = {
    token: { type: String },
    _state: { state: true }, // 'idle' | 'verifying' | 'ok' | 'error'
    _error: { state: true },
  };

  constructor() {
    super();
    this.token = "";
    this._state = "idle";
    this._error = null;
  }

  createRenderRoot() {
    return this;
  }

  connectedCallback() {
    super.connectedCallback();
    if (this.token) this._verify();
  }

  async _verify() {
    this._state = "verifying";
    try {
      await api.verifyEmail(this.token);
      this._state = "ok";
    } catch (err) {
      this._state = "error";
      this._error = err.message;
    }
  }

  render() {
    if (this._state === "verifying") {
      return html`<div class="card"><p>Verifying your email…</p></div>`;
    }
    if (this._state === "ok") {
      return html`
        <div class="card">
          <h2>Email verified</h2>
          <p>
            Your email is confirmed. An operator will review and approve your
            account; once approved you can log in and create API keys.
          </p>
          <p class="mt-2"><a href="#/login">Log in →</a></p>
        </div>
      `;
    }
    if (this._state === "error") {
      return html`
        <div class="card">
          <h2>Verification failed</h2>
          <div class="msg error">${this._error}</div>
          <p class="mt-2">
            Tokens expire after 24 hours. Sign up again or contact the
            operator if you need a fresh link.
          </p>
        </div>
      `;
    }
    return html`<div class="card"><p>No token supplied.</p></div>`;
  }
}
customElements.define("cc-verify-email", CcVerifyEmail);
