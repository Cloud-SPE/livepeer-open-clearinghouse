// Inline SVG icons — Heroicons outline-style, 24x24, strokeWidth=1.5.
// See web/portal/lib/icons.js for the design rationale; admin keeps a
// parallel copy because we deliberately don't share code between SPAs.

import { svg } from "lit";

const base = (path) => svg`<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${path}</svg>`;

export const icon = {
  home: () =>
    base(svg`<path d="M3 9.5 12 3l9 6.5"/><path d="M5 9v11h14V9"/><path d="M9 20v-6h6v6"/>`),
  users: () =>
    base(svg`<circle cx="9" cy="9" r="3.5"/><path d="M2.5 19c1-3.5 4-5.5 6.5-5.5s5.5 2 6.5 5.5"/><circle cx="17" cy="8" r="2.5"/><path d="M16 13.5c2-.2 4 1.5 5 3.5"/>`),
  log: () =>
    base(svg`<path d="M5 4h12a2 2 0 0 1 2 2v14H7a2 2 0 0 1-2-2V4z"/><path d="M9 8h7"/><path d="M9 12h7"/><path d="M9 16h4"/>`),
  deposit: () =>
    base(svg`<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M4 19h16"/>`),
  shield: () =>
    base(svg`<path d="M12 3 4 6v6c0 5 3.5 8.5 8 9 4.5-.5 8-4 8-9V6l-8-3z"/>`),
  hourglass: () =>
    base(svg`<path d="M6 3h12"/><path d="M6 21h12"/><path d="M6 3v3a6 6 0 0 0 12 0V3"/><path d="M6 21v-3a6 6 0 0 1 12 0v3"/>`),
  refresh: () =>
    base(svg`<path d="M21 12a9 9 0 0 1-15.5 6.3"/><path d="M3 12a9 9 0 0 1 15.5-6.3"/><path d="M21 4v6h-6"/><path d="M3 20v-6h6"/>`),
  plus: () =>
    base(svg`<path d="M12 5v14"/><path d="M5 12h14"/>`),
  x: () =>
    base(svg`<path d="m6 6 12 12"/><path d="M18 6 6 18"/>`),
  menu: () =>
    base(svg`<path d="M4 6h16"/><path d="M4 12h16"/><path d="M4 18h16"/>`),
  logout: () =>
    base(svg`<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>`),
  search: () =>
    base(svg`<circle cx="11" cy="11" r="6"/><path d="m20 20-4.3-4.3"/>`),
  cog: () =>
    base(svg`<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1.08-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09a1.65 1.65 0 0 0 1.51-1.08 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>`),
  activity: () =>
    base(svg`<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>`),
};
