import { LitElement, html } from "lit";
import * as api from "/admin/lib/api.js";
import { icon } from "/admin/lib/icons.js";

const ROLE_TINTS = { owner: "info", member: "" };

export class CcOperators extends LitElement {
  static properties = {
    _operators: { state: true },
    _loading: { state: true },
    _error: { state: true },
    _showCreate: { state: true },
    _justCreated: { state: true }, // {operator, raw_token} — shown once
    _rotatedToken: { state: true }, // {operator_id, raw_token} — shown once
    _busy: { state: true },
  };

  constructor() {
    super();
    this._operators = [];
    this._loading = true;
    this._error = null;
    this._showCreate = false;
    this._justCreated = null;
    this._rotatedToken = null;
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
      const list = await api.listOperators();
      this._operators = list.items;
    } catch (err) {
      this._error = err.message;
    } finally {
      this._loading = false;
    }
  }

  async _onCreate(ev) {
    ev.preventDefault();
    this._busy = true;
    this._error = null;
    const data = new FormData(ev.currentTarget);
    try {
      const result = await api.createOperator({
        email: data.get("email"),
        name: data.get("name"),
        role: data.get("role") || "member",
      });
      this._justCreated = result;
      this._showCreate = false;
      await this._refresh();
    } catch (err) {
      this._error = err.message;
    } finally {
      this._busy = false;
    }
  }

  async _revoke(op) {
    if (!confirm(`Revoke operator ${op.email}? Their bearer token stops working immediately.`))
      return;
    this._busy = true;
    this._error = null;
    try {
      await api.revokeOperator(op.id);
      await this._refresh();
    } catch (err) {
      this._error = err.message;
    } finally {
      this._busy = false;
    }
  }

  async _rotate(op) {
    if (
      !confirm(
        `Rotate ${op.email}'s bearer token? The old one stops working immediately. ` +
          "We'll show the new token exactly once.",
      )
    )
      return;
    this._busy = true;
    this._error = null;
    try {
      const result = await api.rotateOperatorToken(op.id);
      this._rotatedToken = { operator_id: op.id, raw_token: result.raw_token };
    } catch (err) {
      this._error = err.message;
    } finally {
      this._busy = false;
    }
  }

  async _setRole(op, role) {
    if (op.role === role) return;
    this._busy = true;
    this._error = null;
    try {
      await api.updateOperator(op.id, { role });
      await this._refresh();
    } catch (err) {
      this._error = err.message;
    } finally {
      this._busy = false;
    }
  }

  _renderCreateModal() {
    if (!this._showCreate) return null;
    return html`
      <div class="modal-backdrop" @click=${(e) => e.target === e.currentTarget && (this._showCreate = false)}>
        <div class="modal-card">
          <h3>New operator</h3>
          <p class="muted">
            We'll generate a bearer token and show it exactly once — copy it
            before closing the dialog.
          </p>
          <form class="form mt-2" @submit=${this._onCreate}>
            <div class="field">
              <label for="op-email">Email</label>
              <input id="op-email" name="email" type="email" required />
            </div>
            <div class="field">
              <label for="op-name">Name</label>
              <input id="op-name" name="name" type="text" required />
            </div>
            <div class="field">
              <label for="op-role">Role</label>
              <select id="op-role" name="role">
                <option value="member">member — full access except managing operators</option>
                <option value="owner">owner — can manage other operators</option>
              </select>
            </div>
            <div class="row">
              <button class="primary" type="submit" ?disabled=${this._busy}>
                ${this._busy ? "Creating…" : "Create"}
              </button>
              <button
                class="ghost"
                type="button"
                @click=${() => (this._showCreate = false)}
              >
                Cancel
              </button>
            </div>
          </form>
        </div>
      </div>
    `;
  }

  _renderJustCreated() {
    if (!this._justCreated) return null;
    const { operator, raw_token } = this._justCreated;
    return html`
      <div class="modal-backdrop">
        <div class="modal-card">
          <h3>Operator created</h3>
          <p>
            <strong>${operator.email}</strong> · <span class="pill info">${operator.role}</span>
          </p>
          <p class="mt-2">
            Bearer token (copy now — it won't be shown again):
          </p>
          <pre class="secret mt-1">${raw_token}</pre>
          <div class="row mt-2">
            <button class="primary" @click=${() => (this._justCreated = null)}>
              Got it
            </button>
          </div>
        </div>
      </div>
    `;
  }

  _renderRotated() {
    if (!this._rotatedToken) return null;
    return html`
      <div class="modal-backdrop">
        <div class="modal-card">
          <h3>Token rotated</h3>
          <p>New bearer token (copy now — it won't be shown again):</p>
          <pre class="secret mt-1">${this._rotatedToken.raw_token}</pre>
          <div class="row mt-2">
            <button class="primary" @click=${() => (this._rotatedToken = null)}>
              Got it
            </button>
          </div>
        </div>
      </div>
    `;
  }

  render() {
    return html`
      <div class="row" style="justify-content: space-between;">
        <h1>Operators</h1>
        <button class="primary" @click=${() => (this._showCreate = true)}>
          ${icon.plus()} New operator
        </button>
      </div>
      <p class="muted">
        Anyone with the bootstrap token is an <code>owner</code>. Owners can
        create and manage other operators; members can do everything else.
        Operator management endpoints require the <code>owner</code> role.
      </p>

      ${this._error ? html`<div class="msg error mb-2">${this._error}</div>` : null}

      <div class="card">
        ${this._loading
          ? html`<p class="muted">Loading…</p>`
          : this._operators.length === 0
            ? html`<p class="muted">No operators yet.</p>`
            : html`
                <table>
                  <thead>
                    <tr>
                      <th>Email</th>
                      <th>Name</th>
                      <th>Role</th>
                      <th>Last login</th>
                      <th>Status</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    ${this._operators.map(
                      (op) => html`
                        <tr>
                          <td><code>${op.email}</code></td>
                          <td>${op.name}</td>
                          <td>
                            <span class="pill ${ROLE_TINTS[op.role] ?? ""}">${op.role}</span>
                          </td>
                          <td class="small muted">
                            ${op.last_login_at
                              ? new Date(op.last_login_at).toLocaleString()
                              : "—"}
                          </td>
                          <td>
                            ${op.revoked_at
                              ? html`<span class="pill bad">revoked</span>`
                              : html`<span class="pill ok">active</span>`}
                          </td>
                          <td class="right">
                            ${op.revoked_at
                              ? null
                              : html`
                                  <div class="row" style="justify-content: flex-end;">
                                    ${op.role === "owner"
                                      ? html`<button
                                          class="ghost"
                                          ?disabled=${this._busy}
                                          @click=${() => this._setRole(op, "member")}
                                          title="Demote to member"
                                        >
                                          ↓ member
                                        </button>`
                                      : html`<button
                                          class="ghost"
                                          ?disabled=${this._busy}
                                          @click=${() => this._setRole(op, "owner")}
                                          title="Promote to owner"
                                        >
                                          ↑ owner
                                        </button>`}
                                    <button
                                      class="ghost"
                                      ?disabled=${this._busy}
                                      @click=${() => this._rotate(op)}
                                      title="Rotate this operator's bearer token"
                                    >
                                      ${icon.refresh()} Rotate
                                    </button>
                                    <button
                                      class="danger"
                                      ?disabled=${this._busy}
                                      @click=${() => this._revoke(op)}
                                      title="Revoke this operator's access"
                                    >
                                      Revoke
                                    </button>
                                  </div>
                                `}
                          </td>
                        </tr>
                      `,
                    )}
                  </tbody>
                </table>
              `}
      </div>

      ${this._renderCreateModal()} ${this._renderJustCreated()} ${this._renderRotated()}
    `;
  }
}
customElements.define("cc-operators", CcOperators);
