import { LitElement, html } from "lit";
import * as api from "/portal/lib/api.js";

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

  _renderHeader() {
    return html`
      <header class="header">
        <div class="brand">PymtHouse</div>
        <nav class="nav">
          ${this._user
            ? html`
                <span class="muted">${this._user.email}</span>
                <button class="ghost" @click=${this._doLogout}>Log out</button>
              `
            : html`
                <a href="#/login">Log in</a>
                <a href="#/signup">Sign up</a>
              `}
        </nav>
      </header>
    `;
  }

  _renderRoute() {
    const { path, params } = this._route;
    if (this._loadingSession) {
      return html`<p class="muted">Loading session…</p>`;
    }

    // verify-email is always reachable, even when logged in.
    if (path === "/verify-email") {
      return html`<cc-verify-email .token=${params.get("token") || ""}></cc-verify-email>`;
    }

    if (!this._user) {
      if (path === "/signup") return html`<cc-signup></cc-signup>`;
      return html`<cc-login></cc-login>`;
    }

    if (path === "/api-keys") return html`<cc-api-keys></cc-api-keys>`;
    return html`<cc-dashboard .user=${this._user}></cc-dashboard>`;
  }

  render() {
    return html`
      ${this._renderHeader()}
      ${this._error ? html`<div class="msg error mb-2">${this._error}</div>` : null}
      <main>${this._renderRoute()}</main>
    `;
  }
}
customElements.define("cc-app", CcApp);
