import { LitElement, html } from "lit";
import * as api from "/admin/lib/api.js";
import { icon } from "/admin/lib/icons.js";

function formatWei(wei) {
  if (wei == null) return "—";
  return String(wei).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

export class CcDeposits extends LitElement {
  static properties = {
    _snapshots: { state: true },
    _loading: { state: true },
    _error: { state: true },
  };

  constructor() {
    super();
    this._snapshots = [];
    this._loading = true;
    this._error = null;
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
      const list = await api.listDepositSnapshots(100);
      this._snapshots = list.items;
    } catch (err) {
      this._error = err.message;
    } finally {
      this._loading = false;
    }
  }

  render() {
    const latest = this._snapshots[0];
    return html`
      <h1>Deposit snapshots</h1>
      ${this._error ? html`<div class="msg error mb-2">${this._error}</div>` : null}

      <div class="metric-grid">
        <div class="metric accent">
          <div class="label">Latest deposit (wei)</div>
          <div class="value mono" style="font-size: 20px;">
            ${formatWei(latest?.deposit_wei)}
          </div>
          <div class="sub">
            ${latest
              ? `Taken ${new Date(latest.taken_at).toLocaleString()}`
              : "No snapshot yet — the poller runs every 5 minutes"}
          </div>
        </div>
        <div class="metric info">
          <div class="label">Latest reserve (wei)</div>
          <div class="value mono" style="font-size: 20px;">
            ${formatWei(latest?.reserve_wei)}
          </div>
          <div class="sub">
            withdraw_round: ${latest?.withdraw_round ?? "—"}
          </div>
        </div>
      </div>

      <div class="card">
        <div class="row" style="justify-content: space-between;">
          <h3 style="margin: 0;">History</h3>
          <button class="ghost" @click=${this._refresh}>
            ${icon.refresh()} Refresh
          </button>
        </div>
        ${this._loading
          ? html`<p class="muted mt-1">Loading…</p>`
          : this._snapshots.length === 0
            ? html`<p class="muted mt-1">No snapshots yet.</p>`
            : html`
                <table class="mt-1">
                  <thead>
                    <tr>
                      <th>When</th>
                      <th class="right">Deposit (wei)</th>
                      <th class="right">Reserve (wei)</th>
                      <th class="right">Withdraw round</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${this._snapshots.map(
                      (s) => html`
                        <tr>
                          <td>${new Date(s.taken_at).toLocaleString()}</td>
                          <td class="right mono">${formatWei(s.deposit_wei)}</td>
                          <td class="right mono">${formatWei(s.reserve_wei)}</td>
                          <td class="right mono">${s.withdraw_round}</td>
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
customElements.define("cc-deposits", CcDeposits);
