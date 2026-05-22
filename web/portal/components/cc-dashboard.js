import { LitElement, html } from "lit";

export class CcDashboard extends LitElement {
  static properties = {
    user: { type: Object },
  };

  constructor() {
    super();
    this.user = null;
  }

  createRenderRoot() {
    return this;
  }

  _approvalPill() {
    if (!this.user) return null;
    if (this.user.approved) {
      return html`<span class="pill ok">Approved</span>`;
    }
    if (!this.user.email_verified_at) {
      return html`<span class="pill warn">Pending email verification</span>`;
    }
    return html`<span class="pill warn">Awaiting operator approval</span>`;
  }

  render() {
    if (!this.user) return null;
    return html`
      <div class="card">
        <h2>Welcome back</h2>
        <p class="muted">${this.user.email}</p>
        <p class="mt-1">Account status: ${this._approvalPill()}</p>
      </div>

      <div class="card">
        <h3>What you can do</h3>
        <ul style="margin-left: 18px;">
          <li>
            <a href="#/api-keys">Manage your API keys</a> — create one to
            authenticate your app developer calls
          </li>
          <li class="muted">Top up your credit balance (operator-only for now)</li>
          <li class="muted">Discover capabilities and orchestrators (via API)</li>
          <li class="muted">Mint payment tickets (via API, coming in a later phase)</li>
        </ul>
      </div>

      ${this.user.approved
        ? null
        : html`
            <div class="card">
              <h3>Approval needed</h3>
              <p>
                An operator must approve your account before you can create
                API keys or call the network. You'll receive an email when
                that happens.
              </p>
            </div>
          `}
    `;
  }
}
customElements.define("cc-dashboard", CcDashboard);
