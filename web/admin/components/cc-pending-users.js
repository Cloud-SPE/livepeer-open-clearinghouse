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
                            <button
                              class="primary"
                              ?disabled=${this._busy || !u.email_verified_at}
                              @click=${() => this._approve(u.id)}
                              title=${u.email_verified_at
                                ? "Approve this user"
                                : "User must verify email first"}
                            >
                              Approve
                            </button>
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
