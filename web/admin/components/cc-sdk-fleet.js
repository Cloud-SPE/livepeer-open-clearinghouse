import { LitElement, html } from "lit";
import * as api from "/admin/lib/api.js";

function statusPill(s) {
  switch (s) {
    case "approved": return "ok";
    case "deprecated": return "warn";
    case "blocked": return "warn";
    case "unknown": return "info";
    default: return "";
  }
}

export class CcSdkFleet extends LitElement {
  static properties = {
    _distribution: { state: true },
    _recentSessions: { state: true },
    _discrepancies: { state: true },
    _shaMismatches: { state: true },
    _hours: { state: true },
    _error: { state: true },
  };

  constructor() {
    super();
    this._distribution = [];
    this._recentSessions = [];
    this._discrepancies = [];
    this._shaMismatches = [];
    this._hours = 24;
    this._error = null;
  }

  createRenderRoot() { return this; }

  connectedCallback() {
    super.connectedCallback();
    this._refresh();
  }

  async _refresh() {
    this._error = null;
    try {
      const [dist, sessions, counts] = await Promise.all([
        api.api(`/sdk-distribution?limit=20`),
        api.api(`/sessions/recent?limit=20`),
        api.api(`/telemetry/event-counts?hours=${this._hours}`),
      ]);
      if (dist.ok) this._distribution = dist.body.items || [];
      if (sessions.ok) this._recentSessions = sessions.body.items || [];
      if (counts.ok) {
        const c = counts.body.counts || {};
        this._discrepancies = c["server.discrepancy_detected"] || 0;
        this._shaMismatches = c["server.sdk_sha_mismatch"] || 0;
      }
    } catch (e) {
      this._error = e?.message || String(e);
    }
  }

  _setHours(h) {
    this._hours = h;
    this._refresh();
  }

  render() {
    return html`
      <section class="card">
        <header class="card-head">
          <h2>SDK fleet</h2>
          <div class="row">
            <button class="btn ${this._hours === 1 ? "primary" : "ghost"}" @click=${() => this._setHours(1)}>1h</button>
            <button class="btn ${this._hours === 24 ? "primary" : "ghost"}" @click=${() => this._setHours(24)}>24h</button>
            <button class="btn ${this._hours === 168 ? "primary" : "ghost"}" @click=${() => this._setHours(168)}>7d</button>
            <button class="btn ghost" @click=${() => this._refresh()}>Refresh</button>
          </div>
        </header>
        ${this._error ? html`<p class="error">${this._error}</p>` : ""}

        <div class="row">
          <div class="kpi">
            <span class="label">SHA mismatches</span>
            <span class="value ${this._shaMismatches > 0 ? "warn" : ""}">${this._shaMismatches}</span>
          </div>
          <div class="kpi">
            <span class="label">Discrepancies</span>
            <span class="value ${this._discrepancies > 0 ? "warn" : ""}">${this._discrepancies}</span>
          </div>
        </div>

        <h3>Version distribution</h3>
        ${this._distribution.length === 0
          ? html`<p class="muted">No SDK-identity activity in the window.</p>`
          : html`
              <table class="table">
                <thead>
                  <tr><th>SDK identity</th><th>Sessions</th><th>Approval</th></tr>
                </thead>
                <tbody>
                  ${this._distribution.map(
                    (d) => html`
                      <tr>
                        <td class="mono small">${d.sdk_identity || "(unattributed)"}</td>
                        <td class="num">${d.count}</td>
                        <td><span class="pill ${statusPill(d.status)}">${d.status}</span></td>
                      </tr>
                    `,
                  )}
                </tbody>
              </table>
            `}

        <h3>Recent sessions</h3>
        ${this._recentSessions.length === 0
          ? html`<p class="muted">No sessions yet.</p>`
          : html`
              <table class="table">
                <thead>
                  <tr>
                    <th>Opened</th>
                    <th>User</th>
                    <th>Capability</th>
                    <th>Mode</th>
                    <th>State</th>
                    <th>SDK</th>
                  </tr>
                </thead>
                <tbody>
                  ${this._recentSessions.map(
                    (s) => html`
                      <tr>
                        <td class="mono small">${s.opened_at}</td>
                        <td class="mono small">${s.user_id}</td>
                        <td>${s.capability}/${s.offering}</td>
                        <td><span class="pill">${s.mode}</span></td>
                        <td><span class="pill ${s.state === "open" ? "ok" : "info"}">${s.state}</span></td>
                        <td>
                          <span class="pill ${statusPill(s.sdk_status)}">${s.sdk_status}</span>
                          <span class="mono small">${s.sdk_identity || "—"}</span>
                        </td>
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

customElements.define("cc-sdk-fleet", CcSdkFleet);
