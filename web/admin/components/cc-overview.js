import { LitElement, html } from "lit";
import * as api from "/admin/lib/api.js";

function formatWei(wei) {
  if (wei == null) return "—";
  return String(wei).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

function timeAgo(iso) {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

export class CcOverview extends LitElement {
  static properties = {
    _users: { state: true },
    _pending: { state: true },
    _audit: { state: true },
    _deposits: { state: true },
    _loading: { state: true },
    _error: { state: true },
  };

  constructor() {
    super();
    this._users = { total: 0, items: [] };
    this._pending = { items: [] };
    this._audit = { items: [] };
    this._deposits = { items: [] };
    this._loading = true;
    this._error = null;
  }

  createRenderRoot() {
    return this;
  }

  async connectedCallback() {
    super.connectedCallback();
    try {
      const [u, p, a, d] = await Promise.all([
        api.listUsers(100, 0),
        api.listPending(),
        api.listAudit(5),
        api.listDepositSnapshots(1),
      ]);
      this._users = u;
      this._pending = p;
      this._audit = a;
      this._deposits = d;
    } catch (err) {
      this._error = err.message;
    } finally {
      this._loading = false;
    }
  }

  _renderMetrics() {
    const approved = this._users.items.filter((u) => u.approved).length;
    const latestDeposit = this._deposits.items[0];
    return html`
      <div class="metric-grid">
        <div class="metric accent">
          <div class="label">Total users</div>
          <div class="value">${this._users.total}</div>
          <div class="sub">${approved} approved</div>
        </div>
        <div class="metric warn">
          <div class="label">Pending approval</div>
          <div class="value">${this._pending.items.length}</div>
          <div class="sub">Awaiting operator action</div>
        </div>
        <div class="metric info">
          <div class="label">Pooled wallet deposit (wei)</div>
          <div class="value mono" style="font-size: 18px;">
            ${formatWei(latestDeposit?.deposit_wei)}
          </div>
          <div class="sub">
            ${latestDeposit
              ? `Snapshot ${timeAgo(latestDeposit.taken_at)}`
              : "No snapshot yet"}
          </div>
        </div>
        <div class="metric">
          <div class="label">Last audit entry</div>
          <div class="value" style="font-size: 14px; font-weight: 500;">
            ${this._audit.items[0]
              ? html`<code>${this._audit.items[0].action}</code>`
              : html`<span class="muted">none</span>`}
          </div>
          <div class="sub">
            ${this._audit.items[0]
              ? `${timeAgo(this._audit.items[0].created_at)} · ${this._audit.items[0].operator_email}`
              : "—"}
          </div>
        </div>
      </div>
    `;
  }

  render() {
    return html`
      <h1>Overview</h1>
      ${this._error ? html`<div class="msg error mb-2">${this._error}</div>` : null}
      ${this._loading ? html`<p class="muted">Loading…</p>` : this._renderMetrics()}
      <div class="card">
        <h3>What to do here</h3>
        <ul style="margin-left: 18px; color: var(--muted);">
          <li><strong>Users</strong> — approve pending users, top up balances, edit per-user spend caps.</li>
          <li><strong>Pending</strong> — focused triage of just the not-yet-approved accounts.</li>
          <li><strong>Audit log</strong> — every state-mutating operator action.</li>
          <li><strong>Deposits</strong> — pooled-wallet on-chain deposit snapshots over time.</li>
        </ul>
      </div>
    `;
  }
}
customElements.define("cc-overview", CcOverview);
