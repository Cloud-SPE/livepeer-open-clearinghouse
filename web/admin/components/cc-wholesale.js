import { LitElement, html } from "lit";
import * as api from "/admin/lib/api.js";
import { formatDateTime, formatEth, timeAgo, toWei } from "/admin/lib/format.js";

const wei = (value) => formatEth(toWei(value) ?? 0n);
const shortAddress = (value) =>
  typeof value === "string" && value.length > 14
    ? `${value.slice(0, 8)}…${value.slice(-6)}`
    : value || "—";

export class CcWholesale extends LitElement {
  static properties = {
    _data: { state: true },
    _loading: { state: true },
    _error: { state: true },
  };

  constructor() {
    super();
    this._data = null;
    this._loading = true;
    this._error = null;
  }

  createRenderRoot() {
    return this;
  }

  connectedCallback() {
    super.connectedCallback();
    this._load();
  }

  async _load() {
    this._loading = true;
    this._error = null;
    try {
      this._data = await api.getWholesaleOverview();
    } catch (err) {
      this._error = err.message;
    } finally {
      this._loading = false;
    }
  }

  _accounts() {
    const rows = this._data?.accounts || [];
    return html`
      <div class="card mt-2">
        <div class="card-head"><h3>Payer–payee accounts</h3></div>
        ${rows.length === 0
          ? html`<p class="empty">No wholesale account has been observed.</p>`
          : html`<div class="table-scroll">
              <table>
                <thead><tr><th>Payee</th><th>Available</th><th>Reserved</th><th>Debited</th><th>Version</th><th>Observed</th><th>State</th></tr></thead>
                <tbody>${rows.map((row) => html`<tr>
                  <td><code title=${row.payee_eth_address}>${shortAddress(row.payee_eth_address)}</code><div class="muted small">${row.broker_url}</div></td>
                  <td class="num">${wei(row.available_value_wei)}</td>
                  <td class="num">${wei(row.reserved_value_wei)}</td>
                  <td class="num">${wei(row.debited_value_wei)}</td>
                  <td class="num">${row.remote_version}</td>
                  <td title=${formatDateTime(row.observed_at)}>${timeAgo(row.observed_at)}</td>
                  <td>${row.over_per_payee_limit ? html`<span class="pill bad">over limit</span>` : row.stale ? html`<span class="pill warn">stale</span>` : html`<span class="pill ok">current</span>`}</td>
                </tr>`)}</tbody>
              </table>
            </div>`}
      </div>`;
  }

  _fundings() {
    const rows = this._data?.fundings || [];
    return html`
      <div class="card mt-2">
        <div class="card-head"><h3>Funding attempts</h3><span class="muted small">Opaque customer correlation only; payment bytes are never displayed.</span></div>
        ${rows.length === 0
          ? html`<p class="empty">No wholesale funding attempts.</p>`
          : html`<div class="table-scroll"><table>
              <thead><tr><th>Mint request</th><th>Shortfall</th><th>Credited</th><th>Status</th><th>Created</th><th>Recovery</th></tr></thead>
              <tbody>${rows.map((row) => html`<tr>
                <td><code>${row.mint_request_id}</code><div class="muted small">${row.correlation_id || "no correlation"}</div></td>
                <td class="num">${wei(row.requested_shortfall_wei)}</td>
                <td class="num">${row.credited_value_wei == null ? "—" : wei(row.credited_value_wei)}</td>
                <td><span class="pill ${row.status === "acknowledged" ? "ok" : "warn"}">${row.status}</span></td>
                <td title=${formatDateTime(row.created_at)}>${timeAgo(row.created_at)}</td>
                <td>${row.needs_attention ? html`<span class="pill bad">attention</span>` : row.has_replayable_payment ? "replayable" : "—"}</td>
              </tr>`)}</tbody>
            </table></div>`}
      </div>`;
  }

  render() {
    if (this._loading && !this._data) return html`<p class="muted">Loading wholesale accounts…</p>`;
    if (this._error) return html`<div class="msg error">${this._error}</div>`;
    const data = this._data;
    const limits = data.limits;
    return html`
      <div class="page-head"><div><h1>Wholesale accounts</h1><p>Shared LOC payer–payee float. Customer balances remain separate.</p></div><button class="ghost" @click=${this._load}>Refresh</button></div>
      ${!limits.enabled ? html`<div class="msg warn">Wholesale routing is disabled.</div>` : null}
      <div class="metrics-grid">
        <div class="metric ${data.aggregate_limit_exceeded ? "alert" : ""}"><div class="label">Projected exposure</div><div class="value">${wei(data.projected_available_wei)}</div><div class="sub">Limit ${wei(limits.max_aggregate_available_wei)}</div></div>
        <div class="metric"><div class="label">Aggregate headroom</div><div class="value">${wei(data.aggregate_headroom_wei)}</div><div class="sub">Target ${wei(limits.target_available_wei)} per payee</div></div>
        <div class="metric ${data.stale_accounts ? "alert-warn" : ""}"><div class="label">Stale accounts</div><div class="value num">${data.stale_accounts}</div><div class="sub">Older than five minutes</div></div>
        <div class="metric ${data.pending_fundings ? "alert-warn" : ""}"><div class="label">Pending funding</div><div class="value num">${data.pending_fundings}</div><div class="sub">Claimed or minted, not acknowledged</div></div>
      </div>
      ${this._accounts()} ${this._fundings()}
    `;
  }
}

customElements.define("cc-wholesale", CcWholesale);
