import { LitElement, html } from "lit";
import * as api from "/portal/lib/api.js";

const LABELS = {
  google: "Continue with Google",
  github: "Continue with GitHub",
};

export class CcOauthButtons extends LitElement {
  static properties = {
    _providers: { state: true },
    _loading: { state: true },
  };

  constructor() {
    super();
    this._providers = [];
    this._loading = true;
  }

  createRenderRoot() {
    return this;
  }

  async connectedCallback() {
    super.connectedCallback();
    try {
      const res = await api.listOAuthProviders();
      this._providers = res.enabled || [];
    } catch {
      this._providers = [];
    } finally {
      this._loading = false;
    }
  }

  render() {
    if (this._loading || this._providers.length === 0) return null;
    return html`
      <div class="row mt-2" style="flex-direction: column; align-items: stretch; gap: 8px;">
        ${this._providers.map(
          (p) => html`
            <a
              class="btn ghost"
              style="text-align: center; display: block;"
              href=${api.oauthLoginUrl(p)}
            >
              ${LABELS[p] || `Continue with ${p}`}
            </a>
          `,
        )}
        <div
          class="muted"
          style="text-align: center; font-size: 12px; padding: 4px 0;"
        >
          or
        </div>
      </div>
    `;
  }
}
customElements.define("cc-oauth-buttons", CcOauthButtons);
