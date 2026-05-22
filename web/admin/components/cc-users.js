import { LitElement, html } from "lit";
import * as api from "/admin/lib/api.js";

function formatWei(wei) {
  if (wei == null) return "—";
  const s = String(wei);
  return s.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

export class CcUsers extends LitElement {
  static properties = {
    _users: { state: true },
    _loading: { state: true },
    _error: { state: true },
    _busy: { state: true },
    _topupTarget: { state: true },
    _topupAmount: { state: true },
    _topupKind: { state: true },
  };

  constructor() {
    super();
    this._users = [];
    this._loading = true;
    this._error = null;
    this._busy = false;
    this._topupTarget = null;
    this._topupAmount = "";
    this._topupKind = "manual";
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
      const list = await api.listUsers(100, 0);
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

  _openTopup(user) {
    this._topupTarget = user;
    this._topupAmount = "";
    this._topupKind = "manual";
  }

  _closeTopup() {
    this._topupTarget = null;
  }

  async _submitTopup(ev) {
    ev.preventDefault();
    const amount = parseInt(this._topupAmount, 10);
    if (!amount || amount <= 0) {
      this._error = "Amount must be a positive integer (wei).";
      return;
    }
    this._busy = true;
    this._error = null;
    try {
      await api.topupUser(this._topupTarget.id, amount, this._topupKind);
      this._topupTarget = null;
      await this._refresh();
    } catch (err) {
      this._error = err.message;
    } finally {
      this._busy = false;
    }
  }

  _renderTopupModal() {
    if (!this._topupTarget) return null;
    return html`
      <div
        class="modal-backdrop"
        style="position: fixed; inset: 0; background: rgba(0,0,0,0.65);
               display: grid; place-items: center; z-index: 10;"
        @click=${this._closeTopup}
      >
        <div
          class="card"
          style="min-width: 340px; max-width: 90vw;"
          @click=${(e) => e.stopPropagation()}
        >
          <h3>Top up ${this._topupTarget.email}</h3>
          <p class="muted">Current balance: ${formatWei(this._topupTarget.balance_wei)} wei</p>
          <form class="form mt-2" @submit=${this._submitTopup}>
            <div class="field">
              <label for="amount">Amount (wei)</label>
              <input
                id="amount"
                type="text"
                inputmode="numeric"
                .value=${this._topupAmount}
                @input=${(e) => (this._topupAmount = e.target.value)}
                required
              />
            </div>
            <div class="field">
              <label for="kind">Kind</label>
              <select
                id="kind"
                .value=${this._topupKind}
                @change=${(e) => (this._topupKind = e.target.value)}
                style="background: var(--surface-2); border: 1px solid var(--border);
                       border-radius: var(--radius); padding: 8px 10px; color: var(--fg);"
              >
                <option value="manual">manual</option>
                <option value="initial">initial</option>
              </select>
            </div>
            ${this._error ? html`<div class="msg error">${this._error}</div>` : null}
            <div class="row" style="justify-content: flex-end;">
              <button
                type="button"
                class="ghost"
                @click=${this._closeTopup}
                ?disabled=${this._busy}
              >
                Cancel
              </button>
              <button type="submit" class="primary" ?disabled=${this._busy}>
                ${this._busy ? "Topping up…" : "Top up"}
              </button>
            </div>
          </form>
        </div>
      </div>
    `;
  }

  render() {
    return html`
      <div class="card">
        <h2>All users</h2>
        <p class="muted">${this._users.length} users · refresh to update</p>
        <div class="mt-2">
          <button class="ghost" @click=${this._refresh}>Refresh</button>
        </div>
      </div>

      ${this._error ? html`<div class="msg error mt-2">${this._error}</div>` : null}

      <div class="card">
        ${this._loading
          ? html`<p class="muted">Loading…</p>`
          : this._users.length === 0
            ? html`<p class="muted">No users yet.</p>`
            : html`
                <table>
                  <thead>
                    <tr>
                      <th>Email</th>
                      <th>Verified</th>
                      <th>Approved</th>
                      <th style="text-align: right;">Balance (wei)</th>
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
                          <td>
                            ${u.approved
                              ? html`<span class="pill">approved</span>`
                              : html`<span class="pill warn">pending</span>`}
                          </td>
                          <td style="text-align: right; font-family: var(--font-mono);">
                            ${formatWei(u.balance_wei)}
                          </td>
                          <td>${new Date(u.created_at).toLocaleDateString()}</td>
                          <td>
                            <div class="row">
                              ${!u.approved && u.email_verified_at
                                ? html`<button
                                    class="primary"
                                    ?disabled=${this._busy}
                                    @click=${() => this._approve(u.id)}
                                  >
                                    Approve
                                  </button>`
                                : null}
                              ${u.approved
                                ? html`<button
                                    class="ghost"
                                    ?disabled=${this._busy}
                                    @click=${() => this._openTopup(u)}
                                  >
                                    Top up
                                  </button>`
                                : null}
                            </div>
                          </td>
                        </tr>
                      `,
                    )}
                  </tbody>
                </table>
              `}
      </div>

      ${this._renderTopupModal()}
    `;
  }
}
customElements.define("cc-users", CcUsers);
