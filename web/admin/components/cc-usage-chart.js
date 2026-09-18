import { LitElement, html, svg } from "lit";
import { formatCount, formatDay, formatEth, formatWei, toWei } from "/admin/lib/format.js";

const E9 = 1_000_000_000n;
const E15 = 1_000_000_000_000_000n;

// Axis unit adapts to the magnitude of the window so a pilot billing a few
// thousand wei a day does not get 18-decimal tick labels.
function pickUnit(max) {
  if (max >= E15) return { name: "ETH", format: (w) => formatEth(w) };
  if (max >= E9) {
    return {
      name: "gwei",
      format: (w) => {
        const whole = w / E9;
        const frac = ((w % E9) * 1000n) / E9;
        const f = frac.toString().padStart(3, "0").replace(/0+$/, "");
        return f ? `${formatWei(whole)}.${f}` : formatWei(whole);
      },
    };
  }
  return { name: "wei", format: (w) => formatWei(w) };
}

// Fill the [start, end] day range so quiet days render as empty bars rather
// than vanishing, which would make a 3-day gap look like a busy week.
function fillDays(byDay, start, end) {
  const map = new Map();
  for (const d of byDay || []) map.set(d.day, d);
  if (!start || !end) return [...map.values()].sort((a, b) => (a.day < b.day ? -1 : 1));
  const out = [];
  const cur = new Date(start);
  cur.setUTCHours(0, 0, 0, 0);
  const last = new Date(end);
  for (let i = 0; i < 400 && cur <= last; i += 1) {
    const key = cur.toISOString().slice(0, 10);
    out.push(map.get(key) || { day: key, jobs: 0, billed_wei: "0" });
    cur.setUTCDate(cur.getUTCDate() + 1);
  }
  return out;
}

// Inline SVG bar chart of billed ETH per day. No library; colours come from
// admin.css tokens via class names so it follows the theme.
export class CcUsageChart extends LitElement {
  static properties = {
    byDay: { attribute: false },
    start: { type: String },
    end: { type: String },
  };

  constructor() {
    super();
    this.byDay = [];
    this.start = null;
    this.end = null;
  }

  createRenderRoot() {
    return this;
  }

  render() {
    const days = fillDays(this.byDay, this.start, this.end);
    if (days.length === 0) {
      return html`<p class="empty">No billed work in this window.</p>`;
    }

    const W = 640;
    const H = 180;
    const padL = 64;
    const padR = 8;
    const padT = 14;
    const padB = 26;
    const innerW = W - padL - padR;
    const innerH = H - padT - padB;

    const wei = days.map((d) => toWei(d.billed_wei) ?? 0n);
    let max = 0n;
    for (const w of wei) if (w > max) max = w;
    const scale = (w) => (max === 0n ? 0 : Number((w * 10000n) / max) / 10000);
    const unit = pickUnit(max);

    const slot = innerW / days.length;
    const barW = Math.max(2, Math.min(48, slot * 0.62));
    const showEvery = days.length > 16 ? Math.ceil(days.length / 8) : 1;

    const gridLevels = [1, 0.5, 0];
    const gridY = (f) => padT + innerH * (1 - f);
    const gridLabel = (f) => {
      if (max === 0n) return f === 0 ? "0" : "";
      const v = (max * BigInt(Math.round(f * 1000))) / 1000n;
      return unit.format(v);
    };

    return html`
      <div class="bar-chart" role="img" aria-label="Billed per day in ${unit.name}">
        ${svg`<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
          ${gridLevels.map(
            (f) => svg`
              <line class="${f === 0 ? "axis" : "grid"}" x1="${padL}" x2="${W - padR}" y1="${gridY(f)}" y2="${gridY(f)}" />
              <text class="tick num" x="${padL - 8}" y="${gridY(f) + 4}" text-anchor="end">${gridLabel(f)}</text>
            `,
          )}
          ${days.map((d, i) => {
            const w = wei[i];
            const h = Math.round(scale(w) * innerH);
            const x = padL + i * slot + (slot - barW) / 2;
            const y = padT + innerH - h;
            const empty = w === 0n;
            const title = `${formatDay(d.day)} · ${formatCount(d.jobs)} job${d.jobs === 1 ? "" : "s"} · ${unit.format(w)} ${unit.name} (${formatWei(w)} wei)`;
            return svg`
              <rect class="bar ${empty ? "empty" : ""}" x="${x}" y="${empty ? padT + innerH - 2 : y}" width="${barW}" height="${empty ? 2 : Math.max(1, h)}" rx="2">
                <title>${title}</title>
              </rect>
              ${i % showEvery === 0
                ? svg`<text class="tick" x="${x + barW / 2}" y="${H - 8}" text-anchor="middle">${formatDay(d.day)}</text>`
                : null}
            `;
          })}
        </svg>`}
      </div>
      <div class="chart-legend">
        <span><span class="swatch"></span>Billed per day (${unit.name})</span>
        <span>${formatCount(days.reduce((n, d) => n + (Number(d.jobs) || 0), 0))} jobs · ${formatEth(wei.reduce((a, b) => a + b, 0n))} ETH total</span>
        <span class="muted">hover a bar for exact wei</span>
      </div>
    `;
  }
}
customElements.define("cc-usage-chart", CcUsageChart);
