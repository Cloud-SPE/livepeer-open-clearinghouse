import { LitElement, html } from "lit";
import * as api from "/admin/lib/api.js";

function eventPill(eventType) {
  if (!eventType) return "info";
  if (eventType.endsWith(".delivered")) return "ok";
  if (eventType.endsWith(".bounced") || eventType.endsWith(".complained")) return "warn";
  if (eventType.endsWith(".failed")) return "warn";
  return "info";
}

export class CcEmailEvents extends LitElement {
  static properties = {
    _events: { state: true },
    _limit: { state: true },
    _error: { state: true },
  };

  constructor() {
    super();
    this._events = [];
    this._limit = 100;
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
      const r = await api.api(`/email/events?limit=${this._limit}`);
      if (r.ok) {
        this._events = r.body.items || [];
      } else {
        this._error = r.body?.detail || `${r.status}`;
      }
    } catch (e) {
      this._error = e?.message || String(e);
    }
  }

  _setLimit(n) {
    this._limit = n;
    this._refresh();
  }

  render() {
    return html`
      <section class="card">
        <header class="card-head">
          <h2>Email events</h2>
          <div class="row">
            <button class="btn ${this._limit === 50 ? "primary" : "ghost"}" @click=${() => this._setLimit(50)}>50</button>
            <button class="btn ${this._limit === 100 ? "primary" : "ghost"}" @click=${() => this._setLimit(100)}>100</button>
            <button class="btn ${this._limit === 500 ? "primary" : "ghost"}" @click=${() => this._setLimit(500)}>500</button>
            <button class="btn ghost" @click=${() => this._refresh()}>Refresh</button>
          </div>
        </header>
        ${this._error ? html`<p class="error">${this._error}</p>` : ""}
        <p class="muted">
          Inbound Resend webhook events, most recent first.
          <code>email_send_id</code> links each event to the original
          outbound send when we can correlate via the provider message
          ID.
        </p>
        ${this._events.length === 0
          ? html`<p class="muted">No webhook events recorded yet.</p>`
          : html`
              <table class="table">
                <thead>
                  <tr>
                    <th>Received</th>
                    <th>Event</th>
                    <th>To</th>
                    <th>Provider message ID</th>
                    <th>Linked send</th>
                  </tr>
                </thead>
                <tbody>
                  ${this._events.map(
                    (e) => html`
                      <tr>
                        <td class="mono small">${e.received_at}</td>
                        <td><span class="pill ${eventPill(e.event_type)}">${e.event_type}</span></td>
                        <td class="mono small">${e.to_address || "—"}</td>
                        <td class="mono small">${e.provider_message_id || "—"}</td>
                        <td class="mono small">${e.email_send_id || "—"}</td>
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

customElements.define("cc-email-events", CcEmailEvents);
