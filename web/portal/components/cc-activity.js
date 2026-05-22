import { LitElement, html } from "lit";
import * as api from "/portal/lib/api.js";
import { icon } from "/portal/lib/icons.js";

function reasonLabel(reason) {
  switch (reason) {
    case "topup":
      return "Topup";
    case "auto_replenish":
      return "Auto replenish";
    case "payment_charge":
      return "Payment";
    case "payment_refund":
      return "Refund";
    case "admin_adjustment":
      return "Adjustment";
    default:
      return reason;
  }
}

function reasonPill(reason) {
  switch (reason) {
    case "topup":
    case "auto_replenish":
      return "ok";
    case "payment_charge":
      return "info";
    case "payment_refund":
      return "warn";
    case "admin_adjustment":
      return "role";
    default:
      return "";
  }
}

export class CcActivity extends LitElement {
  static properties = {
    _balance: { state: true },
    _ledger: { state: true },
    _loading: { state: true },
    _error: { state: true },
  };

  constructor() {
    super();
    this._balance = null;
    this._ledger = [];
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
      const [bal, ledger] = await Promise.all([
        api.getBalance(),
        api.getLedger(100),
      ]);
      this._balance = bal;
      this._ledger = ledger.items;
    } catch (err) {
      this._error = err.message;
    } finally {
      this._loading = false;
    }
  }

  _formatWei(wei) {
    if (wei == null) return "—";
    return String(wei).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  render() {
    return html`
      <h1>Activity</h1>
      ${this._error ? html`<div class="msg error mb-2">${this._error}</div>` : null}

      <div class="metric-grid">
        <div class="metric accent">
          <div class="label">Current balance (wei)</div>
          <div class="value mono">${this._formatWei(this._balance?.amount_wei)}</div>
        </div>
      </div>

      <div class="card">
        <div class="row" style="justify-content: space-between;">
          <h3 style="margin: 0;">Ledger</h3>
          <button class="ghost" @click=${this._refresh}>
            ${icon.refresh()} Refresh
          </button>
        </div>
        ${this._loading
          ? html`<p class="muted mt-1">Loading…</p>`
          : this._ledger.length === 0
            ? html`<p class="muted mt-1">No ledger entries yet.</p>`
            : html`
                <table class="mt-1">
                  <thead>
                    <tr>
                      <th>When</th>
                      <th>Reason</th>
                      <th class="right">Δ (wei)</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${this._ledger.map(
                      (e) => html`
                        <tr>
                          <td>${new Date(e.created_at).toLocaleString()}</td>
                          <td>
                            <span class="pill ${reasonPill(e.reason)}">
                              ${reasonLabel(e.reason)}
                            </span>
                          </td>
                          <td class="right mono">${e.delta_wei}</td>
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
customElements.define("cc-activity", CcActivity);
