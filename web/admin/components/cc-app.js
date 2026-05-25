import { LitElement, html } from "lit";
import * as api from "/admin/lib/api.js";
import { icon } from "/admin/lib/icons.js";

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
    this._tab = "overview";
  }

  createRenderRoot() {
    return this;
  }

  connectedCallback() {
    super.connectedCallback();
    this._onAuth = () => {
      this._hasToken = !!api.getToken();
    };
    this._onTab = (e) => {
      this._tab = e.detail.tab;
    };
    window.addEventListener("cc-admin-auth", this._onAuth);
    window.addEventListener("cc-admin-tab", this._onTab);
    if (this._hasToken) this._verify();
  }

  disconnectedCallback() {
    window.removeEventListener("cc-admin-auth", this._onAuth);
    window.removeEventListener("cc-admin-tab", this._onTab);
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
    this._tab = "overview";
  }

  _toggleSidebar() {
    window.dispatchEvent(new CustomEvent("cc-toggle-sidebar"));
  }

  _renderLogin() {
    return html`
      <div class="page">
        <header class="header">
          <div class="brand">Livepeer Open Clearinghouse · Admin</div>
        </header>
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
      </div>
    `;
  }

  _renderActive() {
    if (this._tab === "overview") return html`<cc-overview></cc-overview>`;
    if (this._tab === "users") return html`<cc-users></cc-users>`;
    if (this._tab === "pending") return html`<cc-pending-users></cc-pending-users>`;
    if (this._tab === "operators") return html`<cc-operators></cc-operators>`;
    if (this._tab === "catalog") return html`<cc-catalog></cc-catalog>`;
    if (this._tab === "audit") return html`<cc-audit-log></cc-audit-log>`;
    if (this._tab === "deposits") return html`<cc-deposits></cc-deposits>`;
    if (this._tab === "telemetry") return html`<cc-telemetry-admin></cc-telemetry-admin>`;
    if (this._tab === "sdk-fleet") return html`<cc-sdk-fleet></cc-sdk-fleet>`;
    if (this._tab === "email") return html`<cc-email-events></cc-email-events>`;
    return html`<cc-overview></cc-overview>`;
  }

  _renderShell() {
    return html`
      <div class="shell">
        <div class="shell-topbar">
          <div class="row">
            <button class="hamburger" @click=${this._toggleSidebar} aria-label="Menu">
              ${icon.menu()}
            </button>
            <span class="brand">Livepeer Open Clearinghouse · Admin</span>
          </div>
          <div class="row">
            <button class="ghost" @click=${this._logout}>
              ${icon.logout()} Log out
            </button>
          </div>
        </div>
        <cc-sidebar .current=${this._tab}></cc-sidebar>
        <main class="shell-content">
          ${this._error ? html`<div class="msg error mb-2">${this._error}</div>` : null}
          ${this._renderActive()}
        </main>
      </div>
    `;
  }

  render() {
    return this._hasToken ? this._renderShell() : this._renderLogin();
  }
}
customElements.define("cc-app", CcApp);
