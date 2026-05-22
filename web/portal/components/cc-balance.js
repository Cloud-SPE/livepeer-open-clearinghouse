import { LitElement, html } from "lit";
import * as api from "/portal/lib/api.js";

function formatWei(wei) {
  if (wei == null) return "—";
  const s = String(wei);
  // Group thousands for readability; treat the value as an integer string.
  return s.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + " wei";
}

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

export class CcBalance extends LitElement {
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
      const [b, l] = await Promise.all([api.getBalance(), api.getLedger(20)]);
      this._balance = b;
      this._ledger = l.items;
    } catch (err) {
      this._error = err.message;
    } finally {
      this._loading = false;
    }
  }

  render() {
    return html`
      <div class="card">
        <h3>Credit balance</h3>
        ${this._loading
          ? html`<p class="muted">Loading…</p>`
          : this._error
            ? html`<div class="msg error">${this._error}</div>`
            : html`
                <p style="font-size: 22px; font-family: var(--font-mono);">
                  ${formatWei(this._balance?.amount_wei)}
                </p>
                <p class="muted">
                  Topups and payments are settled in wei against this balance.
                </p>
              `}
      </div>

      <div class="card">
        <h3>Recent activity</h3>
        ${this._loading
          ? null
          : this._ledger.length === 0
            ? html`<p class="muted">No activity yet.</p>`
            : html`
                <table>
                  <thead>
                    <tr>
                      <th>When</th>
                      <th>Reason</th>
                      <th style="text-align:right;">Delta (wei)</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${this._ledger.map(
                      (e) => html`
                        <tr>
                          <td>${new Date(e.created_at).toLocaleString()}</td>
                          <td>${reasonLabel(e.reason)}</td>
                          <td
                            style="text-align:right; font-family: var(--font-mono);"
                          >
                            ${e.delta_wei}
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
customElements.define("cc-balance", CcBalance);
