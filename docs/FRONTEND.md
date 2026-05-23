# FRONTEND.md

The frontend conventions for Livepeer Open Clearinghouse's two SPAs (`web/portal/` and
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
`/livepeer-modules-openai/web/{site,portal,admin}/` because we want Livepeer Open Clearinghouse
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
  /* Cool zinc base + emerald (portal) / sky (admin) accent. Drawn from
     livepeer-open-clearinghouse's Tailwind zinc-950 / emerald-400 palette but
     restated as plain CSS variables so we keep zero-build. */
  --bg: #09090b;            /* zinc-950 */
  --fg: #fafafa;            /* zinc-50 */
  --muted: #71717a;         /* zinc-500 */
  --muted-2: #52525b;       /* zinc-600 */
  --surface: rgba(24, 24, 27, 0.6);
  --surface-soft: rgba(24, 24, 27, 0.3);
  --surface-hover: rgba(39, 39, 42, 0.5);
  --surface-2: #27272a;     /* zinc-800 solid */
  --border: #27272a;
  --border-strong: #3f3f46;
  --accent: #34d399;        /* emerald-400 in portal; sky-400 in admin */
  --accent-strong: #10b981;
  --accent-fg: #052e16;
  --ok: #34d399;
  --warn: #fbbf24;
  --danger: #f87171;
  --info: #38bdf8;
  --role: #c084fc;
  --radius: 8px;            /* rounded-lg */
  --radius-lg: 12px;        /* rounded-xl */
  --radius-xl: 16px;        /* rounded-2xl */
  --radius-pill: 999px;
}

@layer base { /* fonts, links, body */ }
@layer components { /* .card, .button, .form, .table, .modal, .pill, .msg, .metric */ }
@layer utilities { /* .row, .col, .muted, .mt-1, .mt-2, .mb-2 */ }
```

**Reusable component classes:**

- `.page` — `max-width` container, used as the outermost wrapper inside `<cc-app>`
- `.card`, `.card-soft`, `.card-tight` — surface variants (solid / translucent overlay / tight padding)
- `.primary`, `.ghost`, `.danger` — button variants; all are `inline-flex` so an icon can sit alongside the label
- `.pill`, `.pill.ok`, `.pill.warn`, `.pill.bad`, `.pill.info`, `.pill.role` — status badges with tinted backgrounds
- `.msg`, `.msg.error`, `.msg.ok`, `.msg.warn`, `.msg.compact` — inline status text
- `.modal-backdrop` + `.modal-card` — modals (no library), backdrop is `backdrop-filter: blur(4px)` on rgba black
- `.metric-grid` + `.metric` (`.label`, `.value`, `.sub`; `.metric.accent`/`.warn`/`.info`/`.bad` for color) — dashboard hero cards
- Form: semantic `<form>` + `<label>` (small-caps tracking-wider), inputs styled by default selectors, error `.msg.error` below

## Icons

`lib/icons.js` exports one `icon` object whose values return Lit `svg`
templates. Heroicons-outline style, 24×24 viewport, `strokeWidth=1.5`,
`stroke="currentColor"` so a parent `color: var(--accent)` recolors
everything. Add a new icon by appending an entry — don't reach for an
external library.

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
with a namespaced key (e.g., `livepeer_open_clearinghouse:portal:last-route`).

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
