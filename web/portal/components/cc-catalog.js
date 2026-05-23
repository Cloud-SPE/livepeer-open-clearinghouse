import { LitElement, html } from "lit";
import * as api from "/portal/lib/api.js";
import { icon } from "/portal/lib/icons.js";

const WEI = 1n;
const KWEI = 1000n;
const MWEI = 1000000n;
const GWEI = 1000000000n;
const TWEI = 1000000000000n;
const ETH = 1000000000000000000n;

function formatPrice(weiStr, unit) {
  let w;
  try {
    w = BigInt(weiStr);
  } catch {
    return `${weiStr} wei / ${unit}`;
  }
  if (w === 0n) return `0 wei / ${unit}`;
  // Pick the largest scale that gives an integer-ish number to display.
  const tiers = [
    { div: ETH, label: "ETH" },
    { div: TWEI, label: "Twei" },
    { div: GWEI, label: "Gwei" },
    { div: MWEI, label: "Mwei" },
    { div: KWEI, label: "kwei" },
    { div: WEI, label: "wei" },
  ];
  for (const { div, label } of tiers) {
    if (w >= div) {
      // Show with up to 2 decimals
      const whole = w / div;
      const rem = w % div;
      if (rem === 0n) return `${whole} ${label} / ${unit}`;
      // Best-effort decimal — only matters for non-round amounts
      const decimal = Number(w) / Number(div);
      return `${decimal.toFixed(2)} ${label} / ${unit}`;
    }
  }
  return `${weiStr} wei / ${unit}`;
}

function snippetFor(capName, offeringId, unit) {
  return [
    "// In your code:",
    "const mint = await ph.mintPayment({",
    `  capability: "${capName}",`,
    `  offering: "${offeringId}",`,
    `  workUnits: 1000,   // your budget, in ${unit}`,
    "});",
    "// Send mint.payment_bytes in the Livepeer-Payment header",
    "// to the orch at mint.recipient_eth_address.",
  ].join("\n");
}

export class CcCatalog extends LitElement {
  static properties = {
    _capabilities: { state: true },
    _loading: { state: true },
    _error: { state: true },
    _expanded: { state: true },
    _refreshedAt: { state: true },
  };

  constructor() {
    super();
    this._capabilities = [];
    this._loading = true;
    this._error = null;
    // key = `${capability}::${offeringId}` — expanded rows show the snippet
    this._expanded = new Set();
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
      const data = await api.listCapabilities();
      this._capabilities = data.items;
      this._refreshedAt = new Date();
    } catch (err) {
      this._error = err.message;
    } finally {
      this._loading = false;
    }
  }

  _toggle(key) {
    if (this._expanded.has(key)) {
      this._expanded.delete(key);
    } else {
      this._expanded.add(key);
    }
    this.requestUpdate();
  }

  _renderCapability(cap) {
    const unit = cap.work_unit || "units";
    return html`
      <div class="card">
        <div class="row" style="justify-content: space-between;">
          <div>
            <h3 style="margin: 0;"><code>${cap.name}</code></h3>
            <div class="muted small">
              ${cap.offerings.length} offering${cap.offerings.length === 1 ? "" : "s"}
              · work unit: <code>${unit}</code>
            </div>
          </div>
        </div>
        <table class="mt-1">
          <thead>
            <tr>
              <th>Offering</th>
              <th>Price</th>
              <th style="width: 40px;"></th>
            </tr>
          </thead>
          <tbody>
            ${cap.offerings.map((o) => {
              const key = `${cap.name}::${o.id}`;
              const isExpanded = this._expanded.has(key);
              return html`
                <tr @click=${() => this._toggle(key)} style="cursor: pointer;">
                  <td><code>${o.id}</code></td>
                  <td class="mono">${formatPrice(o.price_per_work_unit_wei, o.work_unit)}</td>
                  <td>${isExpanded ? "▾" : "▸"}</td>
                </tr>
                ${isExpanded
                  ? html`
                      <tr>
                        <td colspan="3" style="background: var(--surface-soft);">
                          <pre style="margin: 0; padding: 8px; font-size: 12px; overflow-x: auto;"><code>${snippetFor(cap.name, o.id, o.work_unit)}</code></pre>
                        </td>
                      </tr>
                    `
                  : null}
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
        Every offering currently advertised by the Livepeer network. Click
        an offering for the exact snippet to call it.
      </p>

      ${this._error ? html`<div class="msg error mb-2">${this._error}</div>` : null}

      ${this._loading && this._capabilities.length === 0
        ? html`<p class="muted">Loading…</p>`
        : this._capabilities.length === 0
          ? html`<div class="card">
              <p class="muted" style="margin: 0;">
                No capabilities advertised right now. This is usually transient —
                refresh in a minute.
              </p>
            </div>`
          : this._capabilities.map((c) => this._renderCapability(c))}
    `;
  }
}
customElements.define("cc-catalog", CcCatalog);
