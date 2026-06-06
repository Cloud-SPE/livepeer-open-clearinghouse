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

function objectOrEmpty(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function prettyJson(value) {
  return JSON.stringify(objectOrEmpty(value), null, 2);
}

function hasExtra(value) {
  return Object.keys(objectOrEmpty(value)).length > 0;
}

function extraKeyCount(value) {
  return Object.keys(objectOrEmpty(value)).length;
}

/**
 * Aggregate orchestrators-per-capability + min/max prices across the
 * catalog. Done client-side because the underlying data already comes
 * back in two well-shaped lists; no point adding a new server-side
 * aggregation just for one tab.
 */
function aggregate(capabilities, orchestrators) {
  // capName -> count of orchs that advertise that capability. The gateway
  // returns each orch's capabilities as an array of objects, not strings,
  // so key on cap.name. (string keys were silently coercing every object
  // to "[object Object]" and producing zeros for every row.)
  const orchCount = new Map();
  for (const o of orchestrators) {
    for (const cap of o.capabilities || []) {
      const key = typeof cap === "string" ? cap : cap.name;
      if (!key) continue;
      orchCount.set(key, (orchCount.get(key) || 0) + 1);
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
    const metadataCount = offerings.reduce((acc, o) => acc + (hasExtra(o.extra) ? 1 : 0), 0);
    return {
      name: c.name,
      unit: c.work_unit || "—",
      offeringCount: offerings.length,
      offerings,
      orchs: orchCount.get(c.name) || 0,
      minWei: min,
      maxWei: max,
      metadataCount,
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
    _expanded: { state: true },
  };

  constructor() {
    super();
    this._rows = [];
    this._orchCount = 0;
    this._loading = true;
    this._error = null;
    this._refreshedAt = null;
    this._expanded = new Set();
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

  _toggle(name) {
    if (this._expanded.has(name)) {
      this._expanded.delete(name);
    } else {
      this._expanded.add(name);
    }
    this.requestUpdate();
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
            ${this._rows.reduce((acc, r) => acc + r.offeringCount, 0)}
          </div>
          <div class="sub">across all capabilities</div>
        </div>
      </div>
    `;
  }

  _renderOfferingDetails(row) {
    return html`
      <tr>
        <td colspan="7" style="background: var(--surface-soft);">
          <div style="display: grid; gap: 12px;">
            ${row.offerings.map(
              (o) => html`
                <div style="border: 1px solid var(--border); border-radius: var(--radius); padding: 10px; background: var(--surface);">
                  <div class="row" style="justify-content: space-between; align-items: flex-start;">
                    <div>
                      <code>${o.id}</code>
                      <div class="muted small">
                        ${formatWei(String(o.price_per_work_unit_wei))} / <code>${o.work_unit || row.unit}</code>
                      </div>
                    </div>
                    <span class="pill ${hasExtra(o.extra) ? "info" : ""}">
                      ${extraKeyCount(o.extra)} metadata key${extraKeyCount(o.extra) === 1 ? "" : "s"}
                    </span>
                  </div>
                  ${hasExtra(o.extra)
                    ? html`
                        <pre style="margin: 10px 0 0; padding: 10px; font-size: 12px; overflow-x: auto; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface-soft);"><code>${prettyJson(o.extra)}</code></pre>
                      `
                    : null}
                </div>
              `,
            )}
          </div>
        </td>
      </tr>
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
              <th class="right">Metadata</th>
            </tr>
          </thead>
          <tbody>
            ${this._rows.map((r) => {
              const isExpanded = this._expanded.has(r.name);
              return html`
                <tr @click=${() => this._toggle(r.name)} style="cursor: pointer;">
                  <td>
                    <span class="muted small" style="display: inline-block; width: 14px;">
                      ${isExpanded ? "▾" : "▸"}
                    </span>
                    <code>${r.name}</code>
                  </td>
                  <td class="right">${r.offeringCount}</td>
                  <td class="right">${r.orchs}</td>
                  <td><code>${r.unit}</code></td>
                  <td class="right mono">
                    ${r.minWei !== null ? formatWei(r.minWei.toString()) : "—"}
                  </td>
                  <td class="right mono">
                    ${r.maxWei !== null ? formatWei(r.maxWei.toString()) : "—"}
                  </td>
                  <td class="right">
                    ${r.metadataCount > 0
                      ? html`<span class="pill info">${r.metadataCount}</span>`
                      : html`<span class="muted">—</span>`}
                  </td>
                </tr>
                ${isExpanded ? this._renderOfferingDetails(r) : null}
              `;
            })}
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
