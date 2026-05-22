import { LitElement, html } from "lit";
import * as api from "/portal/lib/api.js";

function formatWei(wei) {
  if (wei == null) return "—";
  const s = String(wei);
  return s.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
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

export class CcDashboard extends LitElement {
  static properties = {
    user: { type: Object },
    _balance: { state: true },
    _keys: { state: true },
    _ledger: { state: true },
    _loading: { state: true },
    _error: { state: true },
  };

  constructor() {
    super();
    this.user = null;
    this._balance = null;
    this._keys = [];
    this._ledger = [];
    this._loading = true;
    this._error = null;
  }

  createRenderRoot() {
    return this;
  }

  async connectedCallback() {
    super.connectedCallback();
    if (!this.user?.approved) {
      this._loading = false;
      return;
    }
    try {
      const [bal, keys, ledger] = await Promise.all([
        api.getBalance(),
        api.listApiKeys(),
        api.getLedger(5),
      ]);
      this._balance = bal;
      this._keys = keys.items;
      this._ledger = ledger.items;
    } catch (err) {
      this._error = err.message;
    } finally {
      this._loading = false;
    }
  }

  _approvalPill() {
    if (this.user?.approved) {
      return html`<span class="pill ok">Approved</span>`;
    }
    if (!this.user?.email_verified_at) {
      return html`<span class="pill warn">Verify email</span>`;
    }
    return html`<span class="pill warn">Pending approval</span>`;
  }

  _renderMetrics() {
    const activeKeys = this._keys.filter((k) => !k.revoked_at).length;
    const lastPayment = this._ledger.find(
      (e) => e.reason === "payment_charge",
    );
    return html`
      <div class="metric-grid">
        <div class="metric accent">
          <div class="label">Credit balance (wei)</div>
          <div class="value mono">${formatWei(this._balance?.amount_wei)}</div>
          <div class="sub">Settled in wei against your account.</div>
        </div>
        <div class="metric info">
          <div class="label">Active API keys</div>
          <div class="value">${activeKeys}</div>
          <div class="sub">${this._keys.length} total (incl. revoked)</div>
        </div>
        <div class="metric">
          <div class="label">Recent activity</div>
          <div class="value">${this._ledger.length}</div>
          <div class="sub">
            ${lastPayment
              ? `Last payment: ${reasonLabel(lastPayment.reason)}`
              : "No payments yet"}
          </div>
        </div>
        <div class="metric ${this.user?.approved ? "" : "warn"}">
          <div class="label">Account</div>
          <div class="value" style="font-size: 18px;">
            ${this._approvalPill()}
          </div>
          <div class="sub">${this.user?.email}</div>
        </div>
      </div>
    `;
  }

  _renderApprovalCard() {
    if (this.user?.approved) return null;
    return html`
      <div class="card">
        <h3>Approval needed</h3>
        <p class="muted">
          An operator must approve your account before you can create API keys
          or call the network. You'll receive an email when that happens.
        </p>
      </div>
    `;
  }

  _renderRecent() {
    if (!this.user?.approved) return null;
    return html`
      <div class="card">
        <div class="row" style="justify-content: space-between;">
          <h3 style="margin: 0;">Recent activity</h3>
          <a href="#/activity" class="small">View all →</a>
        </div>
        ${this._loading
          ? html`<p class="muted mt-1">Loading…</p>`
          : this._ledger.length === 0
            ? html`<p class="muted mt-1">No activity yet.</p>`
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
                          <td>${reasonLabel(e.reason)}</td>
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

  _renderNextSteps() {
    if (!this.user?.approved) return null;
    return html`
      <div class="card">
        <h3>Next steps</h3>
        <ul style="margin-left: 18px; color: var(--muted);">
          <li>
            <a href="#/api-keys">Manage your API keys</a> — create one per app
            you build.
          </li>
          <li>
            Discover capabilities via <code>GET /v1/capabilities</code>.
          </li>
          <li>
            Mint payments with <code>POST /v1/payments/mint</code> using your
            API key.
          </li>
        </ul>
      </div>
    `;
  }

  render() {
    if (!this.user) return null;
    return html`
      <h1>Dashboard</h1>
      ${this._error ? html`<div class="msg error mb-2">${this._error}</div>` : null}
      ${this._renderMetrics()}
      ${this._renderApprovalCard()}
      ${this._renderRecent()}
      ${this._renderNextSteps()}
    `;
  }
}
customElements.define("cc-dashboard", CcDashboard);
