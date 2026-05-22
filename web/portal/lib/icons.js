// Inline SVG icons — Heroicons outline-style, 24x24, strokeWidth=1.5.
//
// Each export is a Lit `svg` template. Usage:
//
//     import { icon } from "/portal/lib/icons.js";
//     html`<span class="icon">${icon.home()}</span>`
//
// `class="icon"` is a 16x16 inline-flex span (defined in CSS as needed
// per-component); the SVG itself inherits `currentColor` for stroke so
// recolouring is just a parent `color: var(--accent)` away.

import { svg } from "lit";

const base = (path) => svg`<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${path}</svg>`;

export const icon = {
  home: () =>
    base(svg`<path d="M3 9.5 12 3l9 6.5"/><path d="M5 9v11h14V9"/><path d="M9 20v-6h6v6"/>`),
  key: () =>
    base(svg`<circle cx="8" cy="15" r="4"/><path d="M10.85 12.15 21 2"/><path d="m18 5 3 3"/><path d="m15 8 3 3"/>`),
  wallet: () =>
    base(svg`<path d="M21 12V7H5a2 2 0 0 1 0-4h14v4"/><path d="M3 5v14a2 2 0 0 0 2 2h16v-5"/><circle cx="17" cy="14" r="1.5"/>`),
  activity: () =>
    base(svg`<path d="M3 12h4l3-8 4 16 3-8h4"/>`),
  users: () =>
    base(svg`<circle cx="9" cy="9" r="3.5"/><path d="M2.5 19c1-3.5 4-5.5 6.5-5.5s5.5 2 6.5 5.5"/><circle cx="17" cy="8" r="2.5"/><path d="M16 13.5c2-.2 4 1.5 5 3.5"/>`),
  shield: () =>
    base(svg`<path d="M12 3 4 6v6c0 5 3.5 8.5 8 9 4.5-.5 8-4 8-9V6l-8-3z"/>`),
  log: () =>
    base(svg`<path d="M5 4h12a2 2 0 0 1 2 2v14H7a2 2 0 0 1-2-2V4z"/><path d="M9 8h7"/><path d="M9 12h7"/><path d="M9 16h4"/>`),
  deposit: () =>
    base(svg`<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M4 19h16"/>`),
  search: () =>
    base(svg`<circle cx="11" cy="11" r="6"/><path d="m20 20-4.3-4.3"/>`),
  plus: () =>
    base(svg`<path d="M12 5v14"/><path d="M5 12h14"/>`),
  x: () =>
    base(svg`<path d="m6 6 12 12"/><path d="M18 6 6 18"/>`),
  refresh: () =>
    base(svg`<path d="M21 12a9 9 0 0 1-15.5 6.3"/><path d="M3 12a9 9 0 0 1 15.5-6.3"/><path d="M21 4v6h-6"/><path d="M3 20v-6h6"/>`),
  chevronRight: () =>
    base(svg`<path d="m9 6 6 6-6 6"/>`),
  menu: () =>
    base(svg`<path d="M4 6h16"/><path d="M4 12h16"/><path d="M4 18h16"/>`),
  logout: () =>
    base(svg`<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>`),
};
