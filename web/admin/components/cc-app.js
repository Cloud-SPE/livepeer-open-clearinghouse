import { LitElement, html } from "lit";
import * as api from "/admin/lib/api.js";

export class CcApp extends LitElement {
  static properties = {
    _hasToken: { state: true },
    _checking: { state: true },
    _error: { state: true },
    _tab: { state: true },
  };

  constructor() {
    super();
    this._hasToken = !!api.getToken();
    this._checking = false;
    this._error = null;
    this._tab = "users";
  }

  createRenderRoot() {
    return this;
  }

  connectedCallback() {
    super.connectedCallback();
    this._onAuth = () => {
      this._hasToken = !!api.getToken();
    };
    window.addEventListener("cc-admin-auth", this._onAuth);
    if (this._hasToken) this._verify();
  }

  disconnectedCallback() {
    window.removeEventListener("cc-admin-auth", this._onAuth);
    super.disconnectedCallback();
  }

  async _verify() {
    this._checking = true;
    this._error = null;
    try {
      await api.listPending();
    } catch (err) {
      if (err.status === 401) {
        api.clearToken();
        this._error = "Invalid operator token.";
      } else {
        this._error = err.message;
      }
    } finally {
      this._checking = false;
    }
  }

  _saveToken(ev) {
    ev.preventDefault();
    const data = new FormData(ev.currentTarget);
    api.setToken(data.get("token"));
    this._verify();
  }

  _logout() {
    api.clearToken();
  }

  _renderLogin() {
    return html`
      <div class="card">
        <h2>Operator login</h2>
        <p class="muted">
          Paste the bootstrap operator token. It is stored in this browser's
          localStorage and sent as <code>Authorization: Bearer …</code> with
          every admin request.
        </p>
        <form class="form mt-2" @submit=${this._saveToken}>
          <div class="field">
            <label for="token">Bootstrap token</label>
            <input id="token" name="token" type="password" required />
          </div>
          ${this._error ? html`<div class="msg error">${this._error}</div>` : null}
          <button class="primary" type="submit">Continue</button>
        </form>
      </div>
    `;
  }

  _setTab(tab) {
    this._tab = tab;
  }

  _renderTabs() {
    const tabStyle = (active) =>
      active
        ? "border-color: var(--accent); color: var(--accent);"
        : "color: var(--muted);";
    return html`
      <nav class="row" style="gap: 8px; margin-bottom: 16px;">
        <button
          class="ghost"
          style=${tabStyle(this._tab === "users")}
          @click=${() => this._setTab("users")}
        >
          Users
        </button>
        <button
          class="ghost"
          style=${tabStyle(this._tab === "pending")}
          @click=${() => this._setTab("pending")}
        >
          Pending
        </button>
        <button
          class="ghost"
          style=${tabStyle(this._tab === "audit")}
          @click=${() => this._setTab("audit")}
        >
          Audit
        </button>
      </nav>
    `;
  }

  _renderActive() {
    if (this._tab === "audit") return html`<cc-audit-log></cc-audit-log>`;
    if (this._tab === "pending") return html`<cc-pending-users></cc-pending-users>`;
    return html`<cc-users></cc-users>`;
  }

  render() {
    return html`
      <header class="header">
        <div class="brand">PymtHouse · Admin</div>
        <nav class="row">
          ${this._hasToken
            ? html`<button class="ghost" @click=${this._logout}>Log out</button>`
            : null}
        </nav>
      </header>
      <main>
        ${!this._hasToken
          ? this._renderLogin()
          : html`
              ${this._renderTabs()}
              ${this._renderActive()}
            `}
      </main>
    `;
  }
}
customElements.define("cc-app", CcApp);
