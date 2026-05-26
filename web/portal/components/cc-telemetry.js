import { LitElement, html } from "lit";
import * as api from "/portal/lib/api.js";
import { icon } from "/portal/lib/icons.js";

const DEFAULT_WINDOW_HOURS = 24;

function isoDaysAgo(hours) {
  const d = new Date(Date.now() - hours * 3600 * 1000);
  return d.toISOString();
}

function pillClass(eventType) {
  if (eventType.endsWith(".error")) return "warn";
  if (eventType === "session.refill_denied") return "warn";
  if (eventType === "session.refill_granted") return "ok";
  if (eventType === "session.closed") return "info";
  if (eventType.startsWith("server.")) return "role";
  return "";
}

export class CcTelemetry extends LitElement {
  static properties = {
    _items: { state: true },
    _cursor: { state: true },
    _loading: { state: true },
    _error: { state: true },
    _privacy: { state: true },
    _windowHours: { state: true },
  };

  constructor() {
    super();
    this._items = [];
    this._cursor = null;
    this._loading = false;
    this._error = null;
    this._privacy = null;
    this._windowHours = DEFAULT_WINDOW_HOURS;
  }

  createRenderRoot() {
    return this;
  }

  connectedCallback() {
    super.connectedCallback();
    this._refresh();
    this._loadPrivacy();
  }

  async _refresh() {
    this._loading = true;
    this._error = null;
    this._items = [];
    this._cursor = null;
    try {
      const from = encodeURIComponent(isoDaysAgo(this._windowHours));
      const to = encodeURIComponent(new Date().toISOString());
      const res = await api.api(
        `/accounts/me/telemetry/events?from=${from}&to=${to}&page_size=100`,
      );
      if (res.ok) {
        this._items = res.body.items || [];
        this._cursor = res.body.next_cursor || null;
      } else {
        this._error = res.body?.error?.message || res.body?.detail || `HTTP ${res.status}`;
      }
    } catch (e) {
      this._error = e?.message || String(e);
    } finally {
      this._loading = false;
    }
  }

  async _loadMore() {
    if (!this._cursor || this._loading) return;
    this._loading = true;
    try {
      const from = encodeURIComponent(isoDaysAgo(this._windowHours));
      const to = encodeURIComponent(new Date().toISOString());
      const res = await api.api(
        `/accounts/me/telemetry/events?from=${from}&to=${to}&page_size=100&cursor=${encodeURIComponent(this._cursor)}`,
      );
      if (res.ok) {
        this._items = [...this._items, ...(res.body.items || [])];
        this._cursor = res.body.next_cursor || null;
      }
    } finally {
      this._loading = false;
    }
  }

  async _loadPrivacy() {
    try {
      const res = await fetch("/v1/privacy/telemetry");
      if (res.ok) {
        this._privacy = await res.json();
      }
    } catch (e) {
      // best-effort
    }
  }

  _download() {
    // Streaming endpoint — browser handles the download.
    window.location.href = "/v1/accounts/me/telemetry/download";
  }

  _setWindow(hours) {
    this._windowHours = hours;
    this._refresh();
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
            <button class="btn primary" @click=${() => this._download()}>${icon.download()} 30-day download</button>
          </div>
        </header>
        ${this._error
          ? html`<p class="error">${this._error}</p>`
          : html`
              <table class="table">
                <thead>
                  <tr>
                    <th>Received</th>
                    <th>Type</th>
                    <th>Source</th>
                    <th>Correlation</th>
                    <th>Payload</th>
                  </tr>
                </thead>
                <tbody>
                  ${this._items.length === 0 && !this._loading
                    ? html`<tr><td colspan="5" class="muted">No events in window.</td></tr>`
                    : this._items.map(
                        (ev) => html`
                          <tr>
                            <td>${ev.received_ts}</td>
                            <td><span class="pill ${pillClass(ev.event_type)}">${ev.event_type}</span></td>
                            <td>${ev.source}</td>
                            <td class="mono small">${ev.correlation_id ?? "—"}</td>
                            <td class="mono small">${JSON.stringify(ev.payload).slice(0, 200)}</td>
                          </tr>
                        `,
                      )}
                </tbody>
              </table>
              ${this._cursor
                ? html`<button class="btn" ?disabled=${this._loading} @click=${() => this._loadMore()}>Load more</button>`
                : ""}
            `}
        ${this._privacy
          ? html`
              <details class="card-footer">
                <summary>Privacy notice (v${this._privacy.version}, effective ${this._privacy.effective_date})</summary>
                <p><strong>Categories collected:</strong></p>
                <ul>${(this._privacy.categories_collected || []).map((c) => html`<li>${c}</li>`)}</ul>
                <p><strong>Not collected:</strong></p>
                <ul>${(this._privacy.categories_not_collected || []).map((c) => html`<li>${c}</li>`)}</ul>
                <p><strong>Retention:</strong> ${this._privacy.retention_days} days.</p>
                <p><strong>Lawful basis:</strong> ${this._privacy.lawful_basis}</p>
                <p><strong>Data-subject rights:</strong> ${this._privacy.data_subject_rights}</p>
                <p><strong>Contact:</strong> ${this._privacy.contact}</p>
              </details>
            `
          : ""}
      </section>
    `;
  }
}

customElements.define("cc-telemetry", CcTelemetry);
