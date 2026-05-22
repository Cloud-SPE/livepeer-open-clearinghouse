import { LitElement, html } from "lit";
import * as api from "/admin/lib/api.js";

export class CcPendingUsers extends LitElement {
  static properties = {
    _users: { state: true },
    _loading: { state: true },
    _error: { state: true },
    _busy: { state: true },
  };

  constructor() {
    super();
    this._users = [];
    this._loading = true;
    this._error = null;
    this._busy = false;
  }

  createRenderRoot() {
    return this;
  }

  connectedCallback() {
    super.connectedCallback();
    this._refresh();
  }

  async _refresh() {
    this._loading = true;
    this._error = null;
    try {
      const list = await api.listPending();
      this._users = list.items;
    } catch (err) {
      this._error = err.message;
    } finally {
      this._loading = false;
    }
  }

  async _approve(id) {
    this._busy = true;
    this._error = null;
    try {
      await api.approveUser(id);
      await this._refresh();
    } catch (err) {
      this._error = err.message;
    } finally {
      this._busy = false;
    }
  }

  async _approveUnverified(user) {
    const ok = confirm(
      `Approve ${user.email} WITHOUT email verification?\n\n` +
        "The user will be able to log in immediately, but you can't " +
        "prove they control this email address. Only do this for users " +
        "you onboarded out-of-band.",
    );
    if (!ok) return;
    await this._approve(user.id);
  }

  render() {
    return html`
      <div class="card">
        <h2>Pending users</h2>
        <p class="muted">Users that have signed up but are not yet approved.</p>
        <div class="mt-2">
          <button class="ghost" @click=${this._refresh}>Refresh</button>
        </div>
      </div>

      ${this._error ? html`<div class="msg error mt-2">${this._error}</div>` : null}

      <div class="card">
        ${this._loading
          ? html`<p class="muted">Loading…</p>`
          : this._users.length === 0
            ? html`<p class="muted">No pending users.</p>`
            : html`
                <table>
                  <thead>
                    <tr>
                      <th>Email</th>
                      <th>Email verified?</th>
                      <th>Signed up</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    ${this._users.map(
                      (u) => html`
                        <tr>
                          <td>${u.email}</td>
                          <td>
                            ${u.email_verified_at
                              ? html`<span class="pill">verified</span>`
                              : html`<span class="pill warn">pending</span>`}
                          </td>
                          <td>${new Date(u.created_at).toLocaleString()}</td>
                          <td>
                            ${u.email_verified_at
                              ? html`<button
                                  class="primary"
                                  ?disabled=${this._busy}
                                  @click=${() => this._approve(u.id)}
                                  title="Approve this user"
                                >
                                  Approve
                                </button>`
                              : html`<button
                                  class="warn"
                                  ?disabled=${this._busy}
                                  @click=${() => this._approveUnverified(u)}
                                  title="Approve without waiting for email verification — the user will be able to log in immediately"
                                >
                                  Approve unverified
                                </button>`}
                          </td>
                        </tr>
                      `,
                    )}
                  </tbody>
                </table>
              `}
      </div>
    `;
  }
}
customElements.define("cc-pending-users", CcPendingUsers);
