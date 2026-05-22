import { LitElement, html } from "lit";
import * as api from "/admin/lib/api.js";

function shorten(text, n) {
  if (text == null) return "";
  const s = String(text);
  return s.length > n ? s.slice(0, n) + "…" : s;
}

export class CcAuditLog extends LitElement {
  static properties = {
    _entries: { state: true },
    _loading: { state: true },
    _error: { state: true },
  };

  constructor() {
    super();
    this._entries = [];
    this._loading = true;
    this._error = null;
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
      const list = await api.listAudit(200);
      this._entries = list.items;
    } catch (err) {
      this._error = err.message;
    } finally {
      this._loading = false;
    }
  }

  render() {
    return html`
      <div class="card">
        <h2>Operator audit log</h2>
        <p class="muted">
          Every state-mutating operator action. Most-recent first; capped
          at 200 rows per fetch.
        </p>
        <div class="mt-2">
          <button class="ghost" @click=${this._refresh}>Refresh</button>
        </div>
      </div>

      ${this._error ? html`<div class="msg error mt-2">${this._error}</div>` : null}

      <div class="card">
        ${this._loading
          ? html`<p class="muted">Loading…</p>`
          : this._entries.length === 0
            ? html`<p class="muted">No audit entries yet.</p>`
            : html`
                <table>
                  <thead>
                    <tr>
                      <th>When</th>
                      <th>Operator</th>
                      <th>Action</th>
                      <th>Target user</th>
                      <th>Params</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${this._entries.map(
                      (e) => html`
                        <tr>
                          <td>${new Date(e.created_at).toLocaleString()}</td>
                          <td>${e.operator_email}</td>
                          <td><code>${e.action}</code></td>
                          <td>
                            ${e.target_user_email ||
                            (e.target_user_id
                              ? html`<span class="muted">${shorten(e.target_user_id, 8)}…</span>`
                              : html`<span class="muted">—</span>`)}
                          </td>
                          <td>
                            ${e.params
                              ? html`<code style="font-size: 12px;"
                                  >${shorten(JSON.stringify(e.params), 80)}</code
                                >`
                              : html`<span class="muted">—</span>`}
                          </td>
                        </tr>
                      `,
                    )}
                  </tbody>
                </table>
              `}
      </div>
    `;
  }
}
customElements.define("cc-audit-log", CcAuditLog);
