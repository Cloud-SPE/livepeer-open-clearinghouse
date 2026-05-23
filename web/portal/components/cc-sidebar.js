import { LitElement, html } from "lit";
import { icon } from "/portal/lib/icons.js";

const ITEMS = [
  { path: "/", label: "Dashboard", icon: "home" },
  { path: "/api-keys", label: "API keys", icon: "key" },
  { path: "/catalog", label: "Catalog", icon: "search" },
  { path: "/activity", label: "Activity", icon: "activity" },
];

export class CcSidebar extends LitElement {
  static properties = {
    current: { type: String },
    _open: { state: true },
  };

  constructor() {
    super();
    this.current = "/";
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
    this._onClose = () => {
      this._open = false;
    };
    window.addEventListener("cc-toggle-sidebar", this._onToggle);
    window.addEventListener("hashchange", this._onClose);
  }

  disconnectedCallback() {
    window.removeEventListener("cc-toggle-sidebar", this._onToggle);
    window.removeEventListener("hashchange", this._onClose);
    super.disconnectedCallback();
  }

  _go(ev, path) {
    ev.preventDefault();
    if (location.hash !== "#" + path) {
      location.hash = "#" + path;
    }
    this._open = false;
  }

  _renderItem(item) {
    const active = item.path === this.current;
    return html`
      <a
        class="sidebar-item ${active ? "active" : ""}"
        href="#${item.path}"
        @click=${(ev) => this._go(ev, item.path)}
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
        <div class="sidebar-section">Account</div>
        ${ITEMS.map((i) => this._renderItem(i))}
      </aside>
    `;
  }
}
customElements.define("cc-sidebar", CcSidebar);
