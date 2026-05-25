import { LitElement, html } from "lit";
import { icon } from "/admin/lib/icons.js";

const ITEMS = [
  { key: "overview", label: "Overview", icon: "home" },
  { key: "users", label: "Users", icon: "users" },
  { key: "pending", label: "Pending", icon: "hourglass" },
  { key: "operators", label: "Operators", icon: "shield" },
  { key: "catalog", label: "Catalog", icon: "search" },
  { key: "audit", label: "Audit log", icon: "log" },
  { key: "deposits", label: "Deposits", icon: "deposit" },
  { key: "telemetry", label: "Telemetry", icon: "activity" },
  { key: "sdk-fleet", label: "SDK fleet", icon: "shield" },
];

export class CcSidebar extends LitElement {
  static properties = {
    current: { type: String },
    _open: { state: true },
  };

  constructor() {
    super();
    this.current = "overview";
    this._open = false;
  }

  createRenderRoot() {
    return this;
  }

  connectedCallback() {
    super.connectedCallback();
    this._onToggle = () => {
      this._open = !this._open;
    };
    window.addEventListener("cc-toggle-sidebar", this._onToggle);
  }

  disconnectedCallback() {
    window.removeEventListener("cc-toggle-sidebar", this._onToggle);
    super.disconnectedCallback();
  }

  _go(ev, key) {
    ev.preventDefault();
    window.dispatchEvent(
      new CustomEvent("cc-admin-tab", { detail: { tab: key } }),
    );
    this._open = false;
  }

  _renderItem(item) {
    const active = item.key === this.current;
    return html`
      <a
        class="sidebar-item ${active ? "active" : ""}"
        href="#"
        @click=${(ev) => this._go(ev, item.key)}
      >
        ${icon[item.icon]()}
        <span>${item.label}</span>
      </a>
    `;
  }

  render() {
    return html`
      ${this._open
        ? html`<div
            class="shell-sidebar-backdrop"
            @click=${() => (this._open = false)}
          ></div>`
        : null}
      <aside class="shell-sidebar ${this._open ? "open" : ""}">
        <div class="sidebar-section">Operations</div>
        ${ITEMS.map((i) => this._renderItem(i))}
      </aside>
    `;
  }
}
customElements.define("cc-sidebar", CcSidebar);
