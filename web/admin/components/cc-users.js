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
    _configTarget: { state: true },
    _configForm: { state: true },
    _configEffective: { state: true },
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
    this._configTarget = null;
    this._configForm = null;
    this._configEffective = null;
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

  async _openConfig(user) {
    this._configTarget = user;
    this._configForm = null;
    this._configEffective = null;
    this._error = null;
    try {
      const res = await api.getBillingConfig(user.id);
      this._configForm = {
        spend_period_seconds: res.config.spend_period_seconds ?? "",
        spend_period_cap_wei: res.config.spend_period_cap_wei ?? "",
        auto_replenish_increment_wei:
          res.config.auto_replenish_increment_wei ?? "",
        auto_replenish_threshold_wei:
          res.config.auto_replenish_threshold_wei ?? "",
      };
      this._configEffective = res.effective;
    } catch (err) {
      this._error = err.message;
      this._configTarget = null;
    }
  }

  _closeConfig() {
    this._configTarget = null;
    this._configForm = null;
    this._configEffective = null;
  }

  _setConfigField(name, value) {
    this._configForm = { ...this._configForm, [name]: value };
  }

  async _submitConfig(ev) {
    ev.preventDefault();
    if (!this._configForm || !this._configTarget) return;
    const parse = (v) => {
      if (v === "" || v == null) return null;
      const n = parseInt(v, 10);
      return Number.isFinite(n) ? n : null;
    };
    const body = {
      spend_period_seconds: parse(this._configForm.spend_period_seconds),
      spend_period_cap_wei: parse(this._configForm.spend_period_cap_wei),
      auto_replenish_increment_wei: parse(
        this._configForm.auto_replenish_increment_wei,
      ),
      auto_replenish_threshold_wei: parse(
        this._configForm.auto_replenish_threshold_wei,
      ),
    };
    this._busy = true;
    this._error = null;
    try {
      const res = await api.putBillingConfig(this._configTarget.id, body);
      this._configEffective = res.effective;
      // Keep the modal open so the operator sees the new "effective" values.
    } catch (err) {
      this._error = err.message;
    } finally {
      this._busy = false;
    }
  }

  _renderConfigModal() {
    if (!this._configTarget || !this._configForm) return null;
    const f = this._configForm;
    const eff = this._configEffective;
    const field = (label, name, hint) => html`
      <div class="field">
        <label for=${name}>${label}</label>
        <input
          id=${name}
          type="text"
          inputmode="numeric"
          placeholder="(inherit default)"
          .value=${f[name]}
          @input=${(e) => this._setConfigField(name, e.target.value)}
        />
        ${hint
          ? html`<p class="muted" style="font-size: 12px;">${hint}</p>`
          : null}
      </div>
    `;
    return html`
      <div
        style="position: fixed; inset: 0; background: rgba(0,0,0,0.65);
               display: grid; place-items: center; z-index: 10;"
        @click=${this._closeConfig}
      >
        <div
          class="card"
          style="min-width: 480px; max-width: 90vw;"
          @click=${(e) => e.stopPropagation()}
        >
          <h3>Billing config — ${this._configTarget.email}</h3>
          <p class="muted">
            Leave a field blank to inherit the operator-wide default.
          </p>
          <form class="form mt-2" @submit=${this._submitConfig}>
            ${field("Spend period (seconds)", "spend_period_seconds",
              eff ? `effective: ${eff.spend_period_seconds}s` : null)}
            ${field("Spend cap per period (wei)", "spend_period_cap_wei",
              eff ? `effective: ${eff.spend_period_cap_wei} wei` : null)}
            ${field("Auto-replenish increment (wei)", "auto_replenish_increment_wei",
              eff ? `effective: ${eff.auto_replenish_increment_wei} wei` : null)}
            ${field("Auto-replenish threshold (wei)", "auto_replenish_threshold_wei",
              eff ? `effective: ${eff.auto_replenish_threshold_wei} wei` : null)}
            ${this._error ? html`<div class="msg error">${this._error}</div>` : null}
            <div class="row" style="justify-content: flex-end;">
              <button
                type="button"
                class="ghost"
                @click=${this._closeConfig}
                ?disabled=${this._busy}
              >
                Close
              </button>
              <button type="submit" class="primary" ?disabled=${this._busy}>
                ${this._busy ? "Saving…" : "Save"}
              </button>
            </div>
          </form>
        </div>
      </div>
    `;
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
                              ${!u.approved && !u.email_verified_at
                                ? html`<button
                                    class="warn"
                                    title="Approve without waiting for email verification — the user will be able to log in immediately"
                                    ?disabled=${this._busy}
                                    @click=${() => this._approveUnverified(u)}
                                  >
                                    Approve unverified
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
                              ${u.approved
                                ? html`<button
                                    class="ghost"
                                    ?disabled=${this._busy}
                                    @click=${() => this._openConfig(u)}
                                  >
                                    Settings
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
      ${this._renderConfigModal()}
    `;
  }
}
customElements.define("cc-users", CcUsers);
