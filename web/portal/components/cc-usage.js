import { LitElement, html } from "lit";
import * as api from "/portal/lib/api.js";
import { icon } from "/portal/lib/icons.js";
import {
  downloadText,
  formatDateTime,
  formatDateTimeShort,
  formatDuration,
  formatEth,
  formatEthCell,
  formatInt,
  formatWeiExact,
  jobOutcomePill,
  toCsv,
} from "/portal/lib/format.js";

const PAGE_SIZE = 50;
const EXPORT_PAGE_SIZE = 100;
const EXPORT_MAX_ROWS = 5000;

const PRESETS = [
  { id: "24h", label: "24h", ms: 24 * 3600e3 },
  { id: "7d", label: "7d", ms: 7 * 86400e3 },
  { id: "30d", label: "30d", ms: 30 * 86400e3 },
  { id: "custom", label: "Custom" },
];

const CSV_COLUMNS = [
  ["id", (j) => j.id],
  ["opened_at", (j) => j.opened_at],
  ["closed_at", (j) => j.closed_at],
  ["duration_seconds", (j) => j.duration_seconds],
  ["protocol", (j) => j.protocol],
  ["capability", (j) => j.capability],
  ["offering", (j) => j.offering],
  ["api_key_id", (j) => j.api_key_id],
  ["api_key_label", (j) => j.api_key_label],
  ["sdk_identity", (j) => j.sdk_identity],
  ["state", (j) => j.state],
  ["accounting_outcome", (j) => j.accounting_outcome],
  ["work_unit", (j) => j.work_unit],
  ["estimated_units", (j) => j.estimated_units],
  ["max_total_units", (j) => j.max_total_units],
  ["actual_units", (j) => j.actual_units],
  ["funded_value_wei", (j) => j.funded_value_wei],
  ["billed_value_wei", (j) => j.billed_value_wei],
  ["billed_eth", (j) => (j.billed_value_wei == null ? "" : formatEth(j.billed_value_wei, { unit: false }))],
  ["refunded_wei", (j) => j.refunded_wei],
  ["held_wei", (j) => j.held_wei],
];

/** datetime-local input value → RFC3339 (UTC), or undefined when blank. */
function localToIso(value) {
  if (!value) return undefined;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? undefined : d.toISOString();
}

/** Date → value for a datetime-local input (local time, minute precision). */
function isoToLocalInput(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export class CcUsage extends LitElement {
  static properties = {
    _preset: { state: true },
    _customSince: { state: true },
    _customUntil: { state: true },
    _filters: { state: true },
    _offset: { state: true },
    _summary: { state: true },
    _jobs: { state: true },
    _total: { state: true },
    _loadingSummary: { state: true },
    _loadingJobs: { state: true },
    _error: { state: true },
    _exporting: { state: true },
    _exportNote: { state: true },
  };

  constructor() {
    super();
    this._preset = "30d";
    const now = new Date();
    this._customUntil = isoToLocalInput(now);
    this._customSince = isoToLocalInput(new Date(now.getTime() - 7 * 86400e3));
    this._filters = { capability: "", offering: "", api_key_id: "", state: "" };
    this._offset = 0;
    this._summary = null;
    this._jobs = [];
    this._total = 0;
    this._loadingSummary = true;
    this._loadingJobs = true;
    this._error = null;
    this._exporting = false;
    this._exportNote = null;
    this._reqSeq = 0;
  }

  createRenderRoot() {
    return this;
  }

  connectedCallback() {
    super.connectedCallback();
    this._reloadAll();
  }

  // ----- period -----

  _range() {
    if (this._preset === "custom") {
      return { since: localToIso(this._customSince), until: localToIso(this._customUntil) };
    }
    const preset = PRESETS.find((p) => p.id === this._preset) || PRESETS[2];
    return { since: new Date(Date.now() - preset.ms).toISOString(), until: undefined };
  }

  _setPreset(id) {
    if (this._preset === id) return;
    this._preset = id;
    this._offset = 0;
    if (id !== "custom") this._reloadAll();
  }

  _applyCustom(ev) {
    ev.preventDefault();
    const form = ev.currentTarget;
    const data = new FormData(form);
    this._customSince = data.get("since") || "";
    this._customUntil = data.get("until") || "";
    this._offset = 0;
    this._reloadAll();
  }

  // ----- loading -----

  async _reloadAll() {
    this._error = null;
    await Promise.all([this._loadSummary(), this._loadJobs()]);
  }

  async _loadSummary() {
    this._loadingSummary = true;
    try {
      this._summary = await api.getUsageSummary(this._range());
    } catch (err) {
      this._error = err.message;
      this._summary = null;
    } finally {
      this._loadingSummary = false;
    }
  }

  _jobParams(extra = {}) {
    const { since, until } = this._range();
    const f = this._filters;
    return {
      since,
      until,
      capability: f.capability || undefined,
      offering: f.offering || undefined,
      api_key_id: f.api_key_id || undefined,
      state: f.state || undefined,
      ...extra,
    };
  }

  async _loadJobs() {
    const seq = ++this._reqSeq;
    this._loadingJobs = true;
    try {
      const res = await api.listUsageJobs(this._jobParams({ limit: PAGE_SIZE, offset: this._offset }));
      if (seq !== this._reqSeq) return; // a newer request superseded this one
      this._jobs = res.items || [];
      this._total = Number(res.total) || 0;
    } catch (err) {
      if (seq !== this._reqSeq) return;
      this._error = err.message;
      this._jobs = [];
      this._total = 0;
    } finally {
      if (seq === this._reqSeq) this._loadingJobs = false;
    }
  }

  _setFilter(key, value) {
    const next = { ...this._filters, [key]: value };
    // An offering belongs to a capability; clear it when the capability changes.
    if (key === "capability" && value !== this._filters.capability) next.offering = "";
    this._filters = next;
    this._offset = 0;
    this._loadJobs();
  }

  _clearFilters() {
    this._filters = { capability: "", offering: "", api_key_id: "", state: "" };
    this._offset = 0;
    this._loadJobs();
  }

  _page(delta) {
    const next = Math.max(0, this._offset + delta * PAGE_SIZE);
    if (next >= this._total && delta > 0) return;
    this._offset = next;
    this._loadJobs();
  }

  // ----- export -----

  async _exportCsv() {
    this._exporting = true;
    this._exportNote = null;
    try {
      const rows = [];
      let offset = 0;
      let total = Infinity;
      while (rows.length < EXPORT_MAX_ROWS && offset < total) {
        const res = await api.listUsageJobs(this._jobParams({ limit: EXPORT_PAGE_SIZE, offset }));
        const items = res.items || [];
        total = Number(res.total) || 0;
        if (items.length === 0) break;
        rows.push(...items);
        offset += items.length;
      }
      const capped = rows.length > EXPORT_MAX_ROWS;
      const out = capped ? rows.slice(0, EXPORT_MAX_ROWS) : rows;
      const csv = toCsv(
        CSV_COLUMNS.map(([name]) => name),
        out.map((j) => CSV_COLUMNS.map(([, pick]) => pick(j))),
      );
      const { since, until } = this._range();
      const tag = (s) => (s ? s.slice(0, 10) : "now");
      downloadText(`usage-${tag(since)}-to-${tag(until)}.csv`, csv);
      this._exportNote =
        capped || total > EXPORT_MAX_ROWS
          ? `Exported the first ${formatInt(EXPORT_MAX_ROWS)} of ${formatInt(total)} rows. Narrow the period or filters to export the rest.`
          : `Exported ${formatInt(out.length)} row${out.length === 1 ? "" : "s"}.`;
    } catch (err) {
      this._exportNote = `Export failed: ${err.message}`;
    } finally {
      this._exporting = false;
    }
  }

  // ----- render: header + period -----

  _renderPeriod() {
    return html`
      <div class="toolbar">
        <div class="segmented" role="group" aria-label="Period">
          ${PRESETS.map(
            (p) => html`
              <button
                class="ghost small ${this._preset === p.id ? "active" : ""}"
                aria-pressed=${this._preset === p.id}
                @click=${() => this._setPreset(p.id)}
              >
                ${p.label}
              </button>
            `,
          )}
        </div>
        <div class="row">
          <button class="ghost small" @click=${this._reloadAll} ?disabled=${this._loadingJobs}>
            ${icon.refresh()} Refresh
          </button>
          <button class="ghost small" @click=${this._exportCsv} ?disabled=${this._exporting || this._total === 0}>
            ${icon.download()} ${this._exporting ? "Exporting…" : "Export CSV"}
          </button>
        </div>
      </div>
      ${this._preset === "custom"
        ? html`
            <form class="filters mb-2" @submit=${this._applyCustom}>
              <div class="field">
                <label for="usage-since">Since</label>
                <input id="usage-since" name="since" type="datetime-local" .value=${this._customSince} required />
              </div>
              <div class="field">
                <label for="usage-until">Until</label>
                <input id="usage-until" name="until" type="datetime-local" .value=${this._customUntil} />
              </div>
              <button class="primary small" type="submit">Apply</button>
            </form>
          `
        : null}
      ${this._exportNote ? html`<div class="msg compact mb-2">${this._exportNote}</div>` : null}
    `;
  }

  // ----- render: summary strip -----

  _renderSummary() {
    const t = this._summary?.totals;
    const eth = (wei) =>
      html`<div class="value eth num" title=${formatWeiExact(wei)}>${formatEth(wei)}</div>`;
    return html`
      <div class="metric-grid">
        <div class="metric accent">
          <div class="label">Billed</div>
          ${this._loadingSummary ? html`<div class="value muted">…</div>` : eth(t?.billed_wei)}
          <div class="sub">Settled cost of work in this period.</div>
        </div>
        <div class="metric info">
          <div class="label">Held</div>
          ${this._loadingSummary ? html`<div class="value muted">…</div>` : eth(t?.held_wei)}
          <div class="sub">Reserved by jobs still open.</div>
        </div>
        <div class="metric">
          <div class="label">Returned</div>
          ${this._loadingSummary ? html`<div class="value muted">…</div>` : eth(t?.refunded_wei)}
          <div class="sub">Unused funds credited back.</div>
        </div>
        <div class="metric">
          <div class="label">Jobs</div>
          <div class="value num">${this._loadingSummary ? "…" : formatInt(t?.jobs)}</div>
          <div class="sub">
            ${this._loadingSummary
              ? ""
              : `${formatInt(t?.closed_jobs)} settled · ${formatInt(t?.open_jobs)} open`}
          </div>
        </div>
      </div>
    `;
  }

  // ----- render: filters -----

  _renderFilters() {
    const offerings = this._summary?.by_offering || [];
    const caps = [...new Set(offerings.map((o) => o.capability))].sort();
    const f = this._filters;
    const offs = [
      ...new Set(offerings.filter((o) => !f.capability || o.capability === f.capability).map((o) => o.offering)),
    ].sort();
    const keys = [...(this._summary?.by_api_key || [])].sort((a, b) =>
      String(a.label || a.api_key_id).localeCompare(String(b.label || b.api_key_id)),
    );
    // Keep a filter value visible even if the summary no longer lists it.
    const ensure = (list, v) => (v && !list.includes(v) ? [v, ...list] : list);
    const active = Object.values(f).some(Boolean);
    return html`
      <div class="filters mb-2">
        <div class="field">
          <label for="f-capability">Capability</label>
          <select id="f-capability" .value=${f.capability} @change=${(e) => this._setFilter("capability", e.target.value)}>
            <option value="">All</option>
            ${ensure(caps, f.capability).map((c) => html`<option value=${c} ?selected=${c === f.capability}>${c}</option>`)}
          </select>
        </div>
        <div class="field">
          <label for="f-offering">Offering</label>
          <select id="f-offering" .value=${f.offering} @change=${(e) => this._setFilter("offering", e.target.value)}>
            <option value="">All</option>
            ${ensure(offs, f.offering).map((o) => html`<option value=${o} ?selected=${o === f.offering}>${o}</option>`)}
          </select>
        </div>
        <div class="field">
          <label for="f-key">Key</label>
          <select id="f-key" .value=${f.api_key_id} @change=${(e) => this._setFilter("api_key_id", e.target.value)}>
            <option value="">All</option>
            ${keys.map(
              (k) => html`<option value=${k.api_key_id} ?selected=${k.api_key_id === f.api_key_id}>
                ${k.label || k.api_key_id}
              </option>`,
            )}
            ${f.api_key_id && !keys.some((k) => k.api_key_id === f.api_key_id)
              ? html`<option value=${f.api_key_id} selected>${f.api_key_id}</option>`
              : null}
          </select>
        </div>
        <div class="field">
          <label for="f-state">State</label>
          <select id="f-state" .value=${f.state} @change=${(e) => this._setFilter("state", e.target.value)}>
            <option value="">All</option>
            <option value="open" ?selected=${f.state === "open"}>Open</option>
            <option value="closed" ?selected=${f.state === "closed"}>Closed</option>
          </select>
        </div>
        ${active
          ? html`<button class="ghost small" @click=${this._clearFilters}>${icon.x()} Clear</button>`
          : null}
      </div>
    `;
  }

  // ----- render: jobs table -----

  _renderUnits(j) {
    const unit = j.work_unit || "";
    if (j.state === "closed" || j.actual_units != null) {
      if (j.actual_units == null) return html`<span class="muted">—</span>`;
      return html`
        <span class="num">${formatInt(j.actual_units)}</span>
        <span class="sub ellipsis" title=${unit}>${unit}</span>
      `;
    }
    return html`
      <span class="muted num" title="Estimated; max ${formatInt(j.max_total_units)} ${unit}">
        ~${formatInt(j.estimated_units)} <span class="small">est.</span>
      </span>
      <span class="sub ellipsis" title=${unit}>${unit}</span>
    `;
  }

  _renderEth(wei, { muted = false } = {}) {
    if (wei == null) return html`<span class="muted">—</span>`;
    return html`<span class="num ${muted ? "muted" : ""}" title=${formatWeiExact(wei)}>${formatEthCell(wei)}</span>`;
  }

  _renderJobRow(j) {
    const pill = jobOutcomePill(j);
    const isOpen = j.state !== "closed";
    return html`
      <tr>
        <td class="nowrap" title=${formatDateTime(j.opened_at)}>
          ${formatDateTimeShort(j.opened_at)}
          <span class="sub">
            ${isOpen
              ? j.state === "draining"
                ? "draining"
                : "running"
              : j.duration_seconds != null
                ? formatDuration(j.duration_seconds)
                : formatDateTime(j.closed_at)}
          </span>
        </td>
        <td>
          <span class="ellipsis" title=${j.capability}>${j.capability}</span>
          <span class="sub ellipsis" title=${j.offering}>${j.offering}${j.protocol === "paid-session/v1" ? " · session" : ""}</span>
        </td>
        <td>
          <span class="ellipsis" title=${j.api_key_label || j.api_key_id || ""}>
            ${j.api_key_label || html`<code class="small">${String(j.api_key_id || "").slice(0, 8)}</code>`}
          </span>
          ${j.sdk_identity ? html`<span class="sub ellipsis" title=${j.sdk_identity}>${j.sdk_identity}</span>` : null}
        </td>
        <td class="nowrap">${this._renderUnits(j)}</td>
        <td class="right nowrap amount">${this._renderEth(j.funded_value_wei, { muted: !isOpen })}</td>
        <td class="right nowrap amount">
          ${isOpen
            ? html`<span class="muted small" title=${formatWeiExact(j.held_wei)}>held ${formatEthCell(j.held_wei)}</span>`
            : this._renderEth(j.billed_value_wei)}
        </td>
        <td class="right nowrap amount">${this._renderEth(j.refunded_wei, { muted: true })}</td>
        <td class="nowrap">
          <span class="pill ${pill.tone}" title="${j.state} · ${j.accounting_outcome}">${pill.label}</span>
        </td>
      </tr>
    `;
  }

  _renderJobs() {
    const from = this._total === 0 ? 0 : this._offset + 1;
    const to = Math.min(this._offset + this._jobs.length, this._total);
    return html`
      <div class="card">
        <div class="row" style="justify-content: space-between;">
          <h3 style="margin: 0;">Jobs</h3>
          <span class="muted small">${this._loadingJobs ? "Loading…" : `${formatInt(this._total)} in period`}</span>
        </div>
        <div class="mt-2">${this._renderFilters()}</div>
        ${this._loadingJobs && this._jobs.length === 0
          ? html`<p class="muted">Loading…</p>`
          : this._jobs.length === 0
            ? html`
                <p class="muted mt-1">
                  No jobs in this period${Object.values(this._filters).some(Boolean) ? " match these filters" : ""}.
                  When your API keys run paid jobs or sessions, each one appears here with what it
                  cost, what was held while it ran, and what was returned.
                </p>
              `
            : html`
                <div class="table-wrap">
                  <table class="dense">
                    <thead>
                      <tr>
                        <th class="nowrap">When</th>
                        <th>Capability&nbsp;/ offering</th>
                        <th>Key</th>
                        <th class="nowrap">Units</th>
                        <th class="right nowrap">Funded (ETH)</th>
                        <th class="right nowrap">Billed (ETH)</th>
                        <th class="right nowrap">Refund (ETH)</th>
                        <th>State</th>
                      </tr>
                    </thead>
                    <tbody>
                      ${this._jobs.map((j) => this._renderJobRow(j))}
                    </tbody>
                  </table>
                </div>
                <div class="pagination">
                  <span>Showing ${formatInt(from)}–${formatInt(to)} of ${formatInt(this._total)}</span>
                  <span class="row">
                    <button class="ghost small" ?disabled=${this._offset === 0 || this._loadingJobs} @click=${() => this._page(-1)}>
                      ← Previous
                    </button>
                    <button class="ghost small" ?disabled=${to >= this._total || this._loadingJobs} @click=${() => this._page(1)}>
                      Next →
                    </button>
                  </span>
                </div>
              `}
      </div>
    `;
  }

  render() {
    return html`
      <h1>Usage</h1>
      <p class="muted">What ran, what it cost, what is held and what was returned.</p>
      ${this._error ? html`<div class="msg error mb-2">${this._error}</div>` : null}
      ${this._renderPeriod()}
      ${this._renderSummary()}
      ${this._renderJobs()}
    `;
  }
}
customElements.define("cc-usage", CcUsage);
