# FRONTEND.md

The frontend conventions for PymtHouse's two SPAs (`web/portal/` and
`web/admin/`). These are pure preferences; deviating breaks consistency,
not correctness. Stick to them.

## Stack

- **Lit 3.2.x** for component reactivity. Pinned in `index.html` via
  `<script type="importmap">` pointing at `esm.sh`.
- **Vanilla JavaScript** (ES modules). No TypeScript on the frontend.
- **Vanilla CSS** with `@layer reset, base, components, utilities`. CSS
  custom properties at `:root` for design tokens.
- **No build step.** Code runs as-is. Open `index.html`, dev server proxies
  `/v1/*` to the gateway. Production: gateway serves the `web/portal/` and
  `web/admin/` directories as static files.

## Why this stack

Agent edits should be visible immediately. A bundler/transpiler step is
extra surface area an agent has to reason about. `esm.sh` + native ES
modules cover everything we need.

We follow the conventions established in
`/livepeer-modules-openai/web/{site,portal,admin}/` because we want PymtHouse
to feel like a sibling product to anyone navigating between the two repos.

## Directory shape

Each SPA has the same shape:

```
web/portal/
├── index.html              # entry point, importmap, root component tag
├── portal.css              # the single stylesheet for the SPA
├── components/
│   └── cc-*.js             # Lit components, kebab-case, "cc-" prefix
├── lib/
│   └── api.js              # fetch wrapper with auth + error handling
├── dev-server.js           # plain Node HTTP server for local dev
└── package.json            # minimal — just a "dev" script
```

The `admin/` SPA has the same shape with `admin.css` and its own
`dev-server.js`. They are independent SPAs. **Do not share code between them.**
If a pattern is good, copy it. If a component is genuinely shared, lift it
later when the duplication is proven.

## Component conventions

- All components are Lit elements with a `cc-` prefix (component-cluster).
- **Light DOM only.** `createRenderRoot() { return this; }` so styles in the
  single stylesheet apply. No shadow DOM, no `<slot>`.
- `static properties = { ... }` for both public attributes and private
  state (`{ state: true }`).
- Templates are `html` tagged template literals, no JSX.
- Events propagate via `dispatchEvent(new CustomEvent('cc-foo', { detail, bubbles: true, composed: false }))`.

```js
import { html, LitElement } from 'lit';

export class CcExample extends LitElement {
  static properties = {
    value: { type: String },
    _loading: { state: true },
  };

  createRenderRoot() { return this; }

  render() {
    return html`
      <div class="card">
        <p>${this.value}</p>
        <button class="primary" @click=${this._save} ?disabled=${this._loading}>
          Save
        </button>
      </div>
    `;
  }

  async _save() {
    this._loading = true;
    // ...
    this._loading = false;
  }
}
customElements.define('cc-example', CcExample);
```

## CSS conventions

Single stylesheet per SPA, organized with `@layer`:

```css
@layer reset, base, components, utilities;

@layer reset {
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
}

:root {
  --bg: #0b0d10;
  --fg: #e6e9ee;
  --muted: #8a92a0;
  --accent: #4f8cff;
  --accent-fg: #ffffff;
  --surface: #14171c;
  --surface-2: #1c2027;
  --border: #2a2f38;
  --danger: #ff5470;
  --warn: #ffb547;
  --radius: 8px;
}

@layer base { /* fonts, links, body */ }
@layer components { /* .card, .button, .form, .table, .modal, .pill, .msg */ }
@layer utilities { /* .m-1, .p-2, .flex, .gap-2 */ }
```

**Reusable component classes:**

- `.card` — bordered, padded surface
- `.primary`, `.ghost`, `.danger` — button variants
- `.pill`, `.pill.ok`, `.pill.warn`, `.pill.bad` — status badges
- `.msg`, `.msg.error`, `.msg.ok`, `.msg.warn`, `.msg.compact` — inline status text
- `.modal-backdrop`, `.modal-card` — modals (no library)
- Form: semantic `<form>` + `<label>`, `FormData` on submit, error `.msg.error` below

## API client

Each SPA has its own `lib/api.js`. The portal authenticates with the user's
API key (set via session cookie after web login); the admin authenticates
with `Authorization: Bearer <admin-token>` from `localStorage`.

```js
// web/portal/lib/api.js
const BASE = '/v1';

export async function api(path, { method = 'GET', body, headers } = {}) {
  const res = await fetch(BASE + path, {
    method,
    credentials: 'include',           // portal uses cookie session
    headers: {
      'Content-Type': 'application/json',
      ...(headers || {}),
    },
    body: body == null ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    const payload = await res.json().catch(() => ({}));
    throw new Error(payload?.error?.message || payload?.error || `HTTP ${res.status}`);
  }
  return res.json();
}
```

Error shape from the backend is consistent:
`{ error: { code: string, message: string, details?: object } }`.

## Routing

Hash-based. Each SPA reads `window.location.hash`, dispatches to a top-level
component, and listens for `hashchange`. No router library.

```js
function readRoute() {
  const h = location.hash.replace(/^#/, '') || '/';
  return h.split('?')[0];
}
window.addEventListener('hashchange', () => app.requestUpdate());
```

## State

Component-local via Lit reactive properties. **No global store.** Cross-
component signaling via custom events on `window` or the app root
(`cc-login`, `cc-logout`, `cc-balance-changed`, etc.).

When state has to survive a reload, put it in `localStorage` deliberately
with a namespaced key (e.g., `pymthouse:portal:last-route`).

## Dev workflow

```bash
cd web/portal && npm run dev   # serves on :3001, proxies /v1 to :8000
cd web/admin && npm run dev    # serves on :3002, proxies /v1 to :8000
```

`dev-server.js` is plain Node `http` — proxies `/v1/*`, `/api/*`, and
session endpoints to `GATEWAY_URL` (default `http://localhost:8000`),
serves everything else as static.

## What we explicitly avoid

- No React / Vue / Svelte / Solid / Preact.
- No Tailwind / Bootstrap / Tachyons.
- No bundler (Vite, Webpack, Rollup, esbuild, etc.).
- No TypeScript on the frontend.
- No `useState`-style hooks; reactive properties are enough.
- No router library; hashchange is enough.
- No design-system package; the CSS tokens above are the design system.

## Accessibility floor

- Every form input has a `<label>`.
- Every interactive element is keyboard-operable.
- Do not remove the focus outline without providing an equivalent.
- Use semantic HTML (`<button>`, `<nav>`, `<main>`, `<table>`). Don't recreate
  buttons with `<div onclick>`.
- Color contrast at WCAG AA against `--bg` / `--surface`.
