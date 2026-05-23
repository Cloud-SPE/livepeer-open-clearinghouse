import { LitElement, html } from "lit";
import * as api from "/portal/lib/api.js";

export class CcApiKeys extends LitElement {
  static properties = {
    _keys: { state: true },
    _loading: { state: true },
    _error: { state: true },
    _newRaw: { state: true },
    _busy: { state: true },
  };

  constructor() {
    super();
    this._keys = [];
    this._loading = true;
    this._error = null;
    this._newRaw = null;
    this._busy = false;
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
      const list = await api.listApiKeys();
      this._keys = list.items;
    } catch (err) {
      this._error = err.message;
    } finally {
      this._loading = false;
    }
  }

  async _create(ev) {
    ev.preventDefault();
    const form = ev.currentTarget;
    this._busy = true;
    this._error = null;
    const data = new FormData(form);
    try {
      const res = await api.createApiKey(data.get("label"));
      this._newRaw = res.raw_key;
      form.reset();
      await this._refresh();
    } catch (err) {
      this._error = err.message;
    } finally {
      this._busy = false;
    }
  }

  async _revoke(id) {
    if (!confirm("Revoke this API key? This cannot be undone.")) return;
    this._busy = true;
    try {
      await api.revokeApiKey(id);
      await this._refresh();
    } catch (err) {
      this._error = err.message;
    } finally {
      this._busy = false;
    }
  }

  _dismissNewRaw() {
    this._newRaw = null;
  }

  render() {
    return html`
      <div class="card">
        <h2>API keys</h2>
        <p class="muted">
          API keys authenticate your app developer calls to the Livepeer Open Clearinghouse
          API. You are shown the raw key exactly once at creation time —
          store it securely.
        </p>
      </div>

      ${this._newRaw
        ? html`
            <div class="card">
              <h3>New key created</h3>
              <p>
                Copy the key now. Livepeer Open Clearinghouse cannot show it again.
              </p>
              <div class="secret">${this._newRaw}</div>
              <div class="mt-2">
                <button class="primary" @click=${this._dismissNewRaw}>
                  I've copied it
                </button>
              </div>
            </div>
          `
        : null}

      <div class="card">
        <h3>Create a new key</h3>
        <form class="form" @submit=${this._create}>
          <div class="field">
            <label for="label">Label (so you can identify it later)</label>
            <input id="label" name="label" type="text" required maxlength="64" />
          </div>
          ${this._error ? html`<div class="msg error">${this._error}</div>` : null}
          <button class="primary" type="submit" ?disabled=${this._busy}>
            ${this._busy ? "Working…" : "Create key"}
          </button>
        </form>
      </div>

      <div class="card">
        <h3>Your keys</h3>
        ${this._loading
          ? html`<p class="muted">Loading…</p>`
          : this._keys.length === 0
            ? html`<p class="muted">No keys yet.</p>`
            : html`
                <table>
                  <thead>
                    <tr>
                      <th>Label</th>
                      <th>Prefix</th>
                      <th>Created</th>
                      <th>Last used</th>
                      <th>Status</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    ${this._keys.map(
                      (k) => html`
                        <tr>
                          <td>${k.label}</td>
                          <td><code>${k.prefix}…</code></td>
                          <td>${new Date(k.created_at).toLocaleString()}</td>
                          <td>
                            ${k.last_used_at
                              ? new Date(k.last_used_at).toLocaleString()
                              : html`<span class="muted">never</span>`}
                          </td>
                          <td>
                            ${k.revoked_at
                              ? html`<span class="pill bad">revoked</span>`
                              : html`<span class="pill ok">active</span>`}
                          </td>
                          <td>
                            ${k.revoked_at
                              ? null
                              : html`
                                  <button
                                    class="danger"
                                    ?disabled=${this._busy}
                                    @click=${() => this._revoke(k.id)}
                                  >
                                    Revoke
                                  </button>
                                `}
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
customElements.define("cc-api-keys", CcApiKeys);
