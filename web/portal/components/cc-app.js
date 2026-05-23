import { LitElement, html } from "lit";
import * as api from "/portal/lib/api.js";
import { icon } from "/portal/lib/icons.js";

function readRoute() {
  const raw = location.hash.replace(/^#/, "") || "/";
  const [path, query] = raw.split("?");
  const params = new URLSearchParams(query || "");
  return { path, params };
}

export class CcApp extends LitElement {
  static properties = {
    _route: { state: true },
    _user: { state: true },
    _loadingSession: { state: true },
    _error: { state: true },
  };

  constructor() {
    super();
    this._route = readRoute();
    this._user = null;
    this._loadingSession = true;
    this._error = null;
  }

  createRenderRoot() {
    return this;
  }

  connectedCallback() {
    super.connectedCallback();
    this._onHashChange = () => {
      this._route = readRoute();
      this._error = null;
    };
    window.addEventListener("hashchange", this._onHashChange);
    window.addEventListener("cc-login", (e) => {
      this._user = e.detail.user;
      location.hash = "#/";
    });
    window.addEventListener("cc-logout", () => {
      this._user = null;
      location.hash = "#/login";
    });
    this._loadSession();
  }

  disconnectedCallback() {
    window.removeEventListener("hashchange", this._onHashChange);
    super.disconnectedCallback();
  }

  async _loadSession() {
    try {
      this._user = await api.me();
    } catch (err) {
      if (err.status !== 401) this._error = err.message;
      this._user = null;
    } finally {
      this._loadingSession = false;
    }
  }

  async _doLogout() {
    try {
      await api.logout();
    } catch {
      /* ignore */
    }
    window.dispatchEvent(new CustomEvent("cc-logout"));
  }

  _toggleSidebar() {
    window.dispatchEvent(new CustomEvent("cc-toggle-sidebar"));
  }

  _renderAuthRoute() {
    const { path, params } = this._route;
    if (path === "/verify-email") {
      return html`<cc-verify-email .token=${params.get("token") || ""}></cc-verify-email>`;
    }
    if (path === "/reset-password") {
      return html`<cc-reset-password .token=${params.get("token") || ""}></cc-reset-password>`;
    }
    if (path === "/signup") return html`<cc-signup></cc-signup>`;
    if (path === "/forgot-password") return html`<cc-forgot-password></cc-forgot-password>`;
    return html`<cc-login></cc-login>`;
  }

  _renderAppRoute() {
    const { path, params } = this._route;
    // Verify-email/reset-password are reachable while logged in too.
    if (path === "/verify-email") {
      return html`<cc-verify-email .token=${params.get("token") || ""}></cc-verify-email>`;
    }
    if (path === "/reset-password") {
      return html`<cc-reset-password .token=${params.get("token") || ""}></cc-reset-password>`;
    }
    if (path === "/api-keys") return html`<cc-api-keys></cc-api-keys>`;
    if (path === "/catalog") return html`<cc-catalog></cc-catalog>`;
    if (path === "/activity") return html`<cc-activity></cc-activity>`;
    return html`<cc-dashboard .user=${this._user}></cc-dashboard>`;
  }

  _renderShell() {
    return html`
      <div class="shell">
        <div class="shell-topbar">
          <div class="row">
            <button class="hamburger" @click=${this._toggleSidebar} aria-label="Menu">
              ${icon.menu()}
            </button>
            <span class="brand">Livepeer Open Clearinghouse</span>
          </div>
          <div class="row">
            <span class="muted small">${this._user.email}</span>
            <button class="ghost" @click=${this._doLogout}>
              ${icon.logout()} Log out
            </button>
          </div>
        </div>
        <cc-sidebar .current=${this._route.path}></cc-sidebar>
        <main class="shell-content">
          ${this._error ? html`<div class="msg error mb-2">${this._error}</div>` : null}
          ${this._renderAppRoute()}
        </main>
      </div>
    `;
  }

  _renderAuthShell() {
    return html`
      <div class="page">
        <header class="header">
          <div class="brand">Livepeer Open Clearinghouse</div>
          <nav class="nav">
            <a href="#/login">Log in</a>
            <a href="#/signup">Sign up</a>
          </nav>
        </header>
        ${this._error ? html`<div class="msg error mb-2">${this._error}</div>` : null}
        <main>${this._renderAuthRoute()}</main>
      </div>
    `;
  }

  render() {
    if (this._loadingSession) {
      return html`<div class="page"><p class="muted">Loading session…</p></div>`;
    }
    return this._user ? this._renderShell() : this._renderAuthShell();
  }
}
customElements.define("cc-app", CcApp);
