import { LitElement, html } from "lit";
import * as api from "/admin/lib/api.js";

const SERVER_EVENT_TYPES = [
  "server.mint_served",
  "server.mint_refused",
  "server.refill_served",
  "server.refill_denied",
  "server.session_janitor_finalized",
  "server.sdk_sha_mismatch",
  "server.discrepancy_detected",
];

const SDK_EVENT_TYPES = [
  "sdk.init",
  "request.mint_started",
  "request.mint_completed",
  "request.settle_completed",
  "request.completed",
  "request.error",
  "session.opened",
  "session.refill_granted",
  "session.refill_denied",
  "session.closed",
  "session.error",
];

function pillFor(eventType) {
  if (eventType.endsWith(".error")) return "warn";
  if (eventType.includes("refused") || eventType.includes("denied")) return "warn";
  if (eventType.includes("discrepancy") || eventType.includes("sha_mismatch")) return "warn";
  if (eventType.startsWith("server.")) return "role";
  return "info";
}

export class CcTelemetryAdmin extends LitElement {
  static properties = {
    _windowHours: { state: true },
    _counts: { state: true },
    _offenders: { state: true },
    _error: { state: true },
    _loading: { state: true },
  };

  constructor() {
    super();
    this._windowHours = 24;
    this._counts = {};
    this._offenders = [];
    this._error = null;
    this._loading = false;
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
      const [counts, offenders] = await Promise.all([
        api.api(`/telemetry/event-counts?hours=${this._windowHours}`),
        api.api(`/telemetry/rate-limit-offenders?hours=${this._windowHours}&limit=20`),
      ]);
      if (counts.ok) {
        this._counts = counts.body.counts || {};
      } else {
        this._error = counts.body?.detail || `HTTP ${counts.status}`;
      }
      if (offenders.ok) {
        this._offenders = offenders.body.items || [];
      }
    } catch (e) {
      this._error = e?.message || String(e);
    } finally {
      this._loading = false;
    }
  }

  _setWindow(h) {
    this._windowHours = h;
    this._refresh();
  }

  _renderCounts(title, types) {
    const rows = types
      .map((t) => [t, this._counts[t] || 0])
      .filter(([, count]) => count > 0)
      .sort((a, b) => b[1] - a[1]);
    return html`
      <h3>${title}</h3>
      ${rows.length === 0
        ? html`<p class="muted">No events in window.</p>`
        : html`
            <table class="table">
              <thead>
                <tr><th>Event type</th><th>Count</th></tr>
              </thead>
              <tbody>
                ${rows.map(
                  ([t, count]) => html`
                    <tr>
                      <td><span class="pill ${pillFor(t)}">${t}</span></td>
                      <td class="num">${count}</td>
                    </tr>
                  `,
                )}
              </tbody>
            </table>
          `}
    `;
  }

  render() {
    return html`
      <section class="card">
        <header class="card-head">
          <h2>Telemetry</h2>
          <div class="row">
            <button class="btn ${this._windowHours === 1 ? "primary" : "ghost"}" @click=${() => this._setWindow(1)}>1h</button>
            <button class="btn ${this._windowHours === 24 ? "primary" : "ghost"}" @click=${() => this._setWindow(24)}>24h</button>
            <button class="btn ${this._windowHours === 168 ? "primary" : "ghost"}" @click=${() => this._setWindow(168)}>7d</button>
            <button class="btn ghost" @click=${() => this._refresh()}>${this._loading ? "…" : "Refresh"}</button>
          </div>
        </header>
        ${this._error ? html`<p class="error">${this._error}</p>` : ""}
        <div class="grid-2">
          <div>${this._renderCounts("Server events", SERVER_EVENT_TYPES)}</div>
          <div>${this._renderCounts("SDK events", SDK_EVENT_TYPES)}</div>
        </div>
        <h3>Top emitters by API key (last ${this._windowHours}h)</h3>
        ${this._offenders.length === 0
          ? html`<p class="muted">No events in window.</p>`
          : html`
              <table class="table">
                <thead>
                  <tr><th>API key</th><th>Events</th></tr>
                </thead>
                <tbody>
                  ${this._offenders.map(
                    (o) => html`
                      <tr>
                        <td class="mono small">${o.api_key_id ?? "(unattributed)"}</td>
                        <td class="num">${o.count}</td>
                      </tr>
                    `,
                  )}
                </tbody>
              </table>
            `}
      </section>
    `;
  }
}

customElements.define("cc-telemetry-admin", CcTelemetryAdmin);
