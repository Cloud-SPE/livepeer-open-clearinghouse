import { LitElement, html } from "lit";
import * as api from "/portal/lib/api.js";
import { icon } from "/portal/lib/icons.js";
import {
  formatDateTime,
  formatEth,
  formatWeiExact,
  ledgerReasonLabel,
  ledgerReasonTone,
  toWei,
} from "/portal/lib/format.js";

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

  render() {
    return html`
      <h1>Activity</h1>
      ${this._error ? html`<div class="msg error mb-2">${this._error}</div>` : null}

      <div class="metric-grid">
        <div class="metric accent">
          <div class="label">Current balance</div>
          <div class="value eth num" title=${formatWeiExact(this._balance?.amount_wei)}>
            ${formatEth(this._balance?.amount_wei)}
          </div>
          <div class="sub">Includes funds held by open jobs — see <a href="#/usage">Usage</a>.</div>
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
                      <th>What</th>
                      <th class="right">Amount</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${this._ledger.map((e) => {
                      const w = toWei(e.delta_wei);
                      return html`
                        <tr>
                          <td class="nowrap">${formatDateTime(e.created_at)}</td>
                          <td>
                            <span class="pill ${ledgerReasonTone(e.reason)}">
                              ${ledgerReasonLabel(e.reason)}
                            </span>
                          </td>
                          <td class="right num" title=${formatWeiExact(e.delta_wei)}>
                            ${w != null && w > 0n ? "+" : ""}${formatEth(e.delta_wei)}
                          </td>
                        </tr>
                      `;
                    })}
                  </tbody>
                </table>
              `}
      </div>
    `;
  }
}
customElements.define("cc-activity", CcActivity);
