import { LitElement, html } from "lit";
import * as api from "/portal/lib/api.js";

export class CcLogin extends LitElement {
  static properties = {
    _loading: { state: true },
    _error: { state: true },
  };

  constructor() {
    super();
    this._loading = false;
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
      const res = await api.login(data.get("email"), data.get("password"));
      window.dispatchEvent(
        new CustomEvent("cc-login", { detail: { user: res.user } }),
      );
    } catch (err) {
      this._error = err.message;
    } finally {
      this._loading = false;
    }
  }

  render() {
    return html`
      <div class="card">
        <h2>Log in</h2>
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
        </p>
      </div>
    `;
  }
}
customElements.define("cc-login", CcLogin);
