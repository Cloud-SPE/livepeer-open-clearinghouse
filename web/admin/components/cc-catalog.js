import { LitElement, html } from "lit";
import * as api from "/admin/lib/api.js";
import { icon } from "/admin/lib/icons.js";

const WEI = 1n;
const KWEI = 1000n;
const MWEI = 1000000n;
const GWEI = 1000000000n;
const TWEI = 1000000000000n;
const ETH = 1000000000000000000n;

function formatWei(weiStr) {
  let w;
  try {
    w = BigInt(weiStr);
  } catch {
    return weiStr;
  }
  if (w === 0n) return "0";
  const tiers = [
    { div: ETH, label: "ETH" },
    { div: TWEI, label: "Twei" },
    { div: GWEI, label: "Gwei" },
    { div: MWEI, label: "Mwei" },
    { div: KWEI, label: "kwei" },
  ];
  for (const { div, label } of tiers) {
    if (w >= div) {
      const dec = Number(w) / Number(div);
      return `${dec.toFixed(dec >= 100 ? 0 : 2)} ${label}`;
    }
  }
  return `${weiStr} wei`;
}

/**
 * Aggregate orchestrators-per-capability + min/max prices across the
 * catalog. Done client-side because the underlying data already comes
 * back in two well-shaped lists; no point adding a new server-side
 * aggregation just for one tab.
 */
function aggregate(capabilities, orchestrators) {
  // capName -> count of orchs
  const orchCount = new Map();
  for (const o of orchestrators) {
    for (const cap of o.capabilities) {
      orchCount.set(cap, (orchCount.get(cap) || 0) + 1);
    }
  }
  return capabilities.map((c) => {
    const offerings = c.offerings;
    const prices = offerings.map((o) => {
      try {
        return BigInt(o.price_per_work_unit_wei);
      } catch {
        return null;
      }
    }).filter((p) => p !== null);
    const min = prices.length ? prices.reduce((a, b) => (a < b ? a : b)) : null;
    const max = prices.length ? prices.reduce((a, b) => (a > b ? a : b)) : null;
    return {
      name: c.name,
      unit: c.work_unit || "—",
      offerings: offerings.length,
      orchs: orchCount.get(c.name) || 0,
      minWei: min,
      maxWei: max,
    };
  });
}

export class CcCatalog extends LitElement {
  static properties = {
    _rows: { state: true },
    _orchCount: { state: true },
    _loading: { state: true },
    _error: { state: true },
    _refreshedAt: { state: true },
  };

  constructor() {
    super();
    this._rows = [];
    this._orchCount = 0;
    this._loading = true;
    this._error = null;
    this._refreshedAt = null;
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
      const [caps, orchs] = await Promise.all([
        api.listCapabilities(),
        api.listOrchestrators(),
      ]);
      this._rows = aggregate(caps.items, orchs.items);
      this._orchCount = orchs.items.length;
      this._refreshedAt = new Date();
    } catch (err) {
      this._error = err.message;
    } finally {
      this._loading = false;
    }
  }

  _renderMetrics() {
    return html`
      <div class="metric-grid">
        <div class="metric accent">
          <div class="label">Capabilities</div>
          <div class="value">${this._rows.length}</div>
          <div class="sub">advertised right now</div>
        </div>
        <div class="metric info">
          <div class="label">Orchestrators</div>
          <div class="value">${this._orchCount}</div>
          <div class="sub">resolving for at least one cap</div>
        </div>
        <div class="metric">
          <div class="label">Total offerings</div>
          <div class="value">
            ${this._rows.reduce((acc, r) => acc + r.offerings, 0)}
          </div>
          <div class="sub">across all capabilities</div>
        </div>
      </div>
    `;
  }

  _renderTable() {
    if (this._rows.length === 0) {
      return html`<div class="card">
        <p class="muted" style="margin: 0;">No capabilities advertised right now.</p>
      </div>`;
    }
    return html`
      <div class="card">
        <table>
          <thead>
            <tr>
              <th>Capability</th>
              <th class="right">Offerings</th>
              <th class="right">Orchs</th>
              <th>Unit</th>
              <th class="right">Min price</th>
              <th class="right">Max price</th>
            </tr>
          </thead>
          <tbody>
            ${this._rows.map(
              (r) => html`
                <tr>
                  <td><code>${r.name}</code></td>
                  <td class="right">${r.offerings}</td>
                  <td class="right">${r.orchs}</td>
                  <td><code>${r.unit}</code></td>
                  <td class="right mono">
                    ${r.minWei !== null ? formatWei(r.minWei.toString()) : "—"}
                  </td>
                  <td class="right mono">
                    ${r.maxWei !== null ? formatWei(r.maxWei.toString()) : "—"}
                  </td>
                </tr>
              `,
            )}
          </tbody>
        </table>
      </div>
    `;
  }

  render() {
    return html`
      <div class="row" style="justify-content: space-between;">
        <h1>Catalog</h1>
        <div class="row">
          ${this._refreshedAt
            ? html`<span class="muted small">
                refreshed ${this._refreshedAt.toLocaleTimeString()}
              </span>`
            : null}
          <button class="ghost" @click=${this._refresh} ?disabled=${this._loading}>
            ${icon.refresh()} ${this._loading ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </div>
      <p class="muted">
        Live view of what the Livepeer network is offering through this gateway's
        service-registry-daemon. Capabilities, offerings, and the per-capability
        orchestrator count + price range, aggregated client-side.
      </p>

      ${this._error ? html`<div class="msg error mb-2">${this._error}</div>` : null}
      ${this._loading && this._rows.length === 0
        ? html`<p class="muted">Loading…</p>`
        : html`${this._renderMetrics()} ${this._renderTable()}`}
    `;
  }
}
customElements.define("cc-catalog", CcCatalog);
