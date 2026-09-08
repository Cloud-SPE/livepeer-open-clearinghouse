import { LitElement, html } from "lit";
import * as api from "/admin/lib/api.js";
import { icon } from "/admin/lib/icons.js";
import {
  eth,
  formatCount,
  formatDateTime,
  toWei,
} from "/admin/lib/format.js";

const PRESETS = [
  { key: "24h", label: "24h", hours: 24 },
  { key: "7d", label: "7d", hours: 24 * 7 },
  { key: "30d", label: "30d", hours: 24 * 30 },
  { key: "custom", label: "Custom", hours: null },
];

const PAGE_SIZE = 50;

// Lifecycle states are a server-side filter; accounting outcomes are applied
// client-side over the loaded page (the API contract only filters on state).
const STATES = ["open", "draining", "closed"];
const OUTCOMES = [
  { key: "broker_settled", label: "settled" },
  { key: "unresolved", label: "unresolved" },
  { key: "conservative_full_charge", label: "conservative charge" },
  { key: "open", label: "open" },
];

function isoToLocalInput(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function localInputToIso(value) {
  if (!value) return null;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

export class CcUsageAdmin extends LitElement {
  static properties = {
    _preset: { state: true },
    _since: { state: true },
    _until: { state: true },
    _customSince: { state: true },
    _customUntil: { state: true },
    _summary: { state: true },
    _jobs: { state: true },
    _offset: { state: true },
    _fUser: { state: true },
    _fCapability: { state: true },
    _fState: { state: true },
    _fOutcome: { state: true },
    _fEmail: { state: true },
    _loadingSummary: { state: true },
    _loadingJobs: { state: true },
    _error: { state: true },
  };

  constructor() {
    super();
    this._preset = "7d";
    this._since = null;
    this._until = null;
    this._customSince = "";
    this._customUntil = "";
    this._summary = null;
    this._jobs = { items: [], total: 0 };
    this._offset = 0;
    this._fUser = null; // { user_id, email }
    this._fCapability = "";
    this._fState = "";
    this._fOutcome = "";
    this._fEmail = "";
    this._loadingSummary = false;
    this._loadingJobs = false;
    this._error = null;
  }

  createRenderRoot() {
    return this;
  }

  connectedCallback() {
    super.connectedCallback();
    this._applyPreset("7d");
  }

  // --- period -------------------------------------------------------------

  _applyPreset(key) {
    this._preset = key;
    const preset = PRESETS.find((p) => p.key === key);
    if (preset && preset.hours) {
      const until = new Date();
      const since = new Date(until.getTime() - preset.hours * 3600 * 1000);
      this._since = since.toISOString();
      this._until = until.toISOString();
      this._customSince = isoToLocalInput(this._since);
      this._customUntil = isoToLocalInput(this._until);
      this._reloadAll();
    } else if (!this._customSince) {
      // First time into custom: seed from the current window so the inputs
      // aren't blank.
      this._customSince = isoToLocalInput(this._since);
      this._customUntil = isoToLocalInput(this._until);
    }
  }

  _applyCustom(ev) {
    ev.preventDefault();
    const since = localInputToIso(this._customSince);
    const until = localInputToIso(this._customUntil) || new Date().toISOString();
    if (!since) {
      this._error = "Custom range needs a start time.";
      return;
    }
    if (since >= until) {
      this._error = "Custom range start must be before its end.";
      return;
    }
    this._error = null;
    this._since = since;
    this._until = until;
    this._reloadAll();
  }

  _periodParams() {
    return { since: this._since, until: this._until };
  }

  // --- loading ------------------------------------------------------------

  _refresh() {
    // Presets are relative to "now": refreshing slides the window forward.
    if (this._preset !== "custom") this._applyPreset(this._preset);
    else this._reloadAll();
  }

  async _reloadAll() {
    this._offset = 0;
    await Promise.all([this._loadSummary(), this._loadJobs()]);
  }

  async _loadSummary() {
    this._loadingSummary = true;
    try {
      this._summary = await api.getFleetUsageSummary(this._periodParams());
      this._error = null;
    } catch (err) {
      this._error = err.message;
    } finally {
      this._loadingSummary = false;
    }
  }

  async _loadJobs() {
    this._loadingJobs = true;
    try {
      this._jobs = await api.listFleetUsageJobs({
        ...this._periodParams(),
        limit: PAGE_SIZE,
        offset: this._offset,
        user_id: this._fUser?.user_id || null,
        capability: this._fCapability || null,
        state: this._fState || null,
      });
      this._error = null;
    } catch (err) {
      this._error = err.message;
      this._jobs = { items: [], total: 0 };
    } finally {
      this._loadingJobs = false;
    }
  }

  _setServerFilter(name, value) {
    this[name] = value;
    this._offset = 0;
    this._loadJobs();
  }

  _filterByUser(row) {
    this._fUser = row ? { user_id: row.user_id, email: row.email } : null;
    this._offset = 0;
    this._loadJobs();
    this.querySelector("#jobs")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  _page(delta) {
    const next = Math.max(0, this._offset + delta * PAGE_SIZE);
    if (next >= (this._jobs.total || 0) && delta > 0) return;
    this._offset = next;
    this._loadJobs();
  }

  // --- derived ------------------------------------------------------------

  _visibleJobs() {
    let items = this._jobs.items || [];
    const email = this._fEmail.trim().toLowerCase();
    if (email) {
      items = items.filter((j) => (j.user_email || "").toLowerCase().includes(email));
    }
    if (this._fOutcome) {
      items = items.filter((j) => j.accounting_outcome === this._fOutcome);
    }
    return items;
  }

  _capabilities() {
    const set = new Set();
    for (const o of this._summary?.by_offering || []) if (o.capability) set.add(o.capability);
    if (this._fCapability) set.add(this._fCapability);
    return [...set].sort();
  }

  _usersSorted() {
    const rows = [...(this._summary?.by_user || [])];
    rows.sort((a, b) => {
      const av = toWei(a.billed_wei) ?? 0n;
      const bv = toWei(b.billed_wei) ?? 0n;
      if (av === bv) return (b.jobs || 0) - (a.jobs || 0);
      return av > bv ? -1 : 1;
    });
    return rows;
  }

  // --- render -------------------------------------------------------------

  _renderPeriod() {
    return html`
      <div class="card-head">
        <h1 style="margin: 0;">Usage</h1>
        <div class="row" style="flex-wrap: wrap;">
          <div class="preset-group" role="group" aria-label="Period">
            ${PRESETS.map(
              (p) => html`<button
                type="button"
                class=${this._preset === p.key ? "active" : ""}
                aria-pressed=${this._preset === p.key}
                @click=${() => this._applyPreset(p.key)}
              >
                ${p.label}
              </button>`,
            )}
          </div>
          <button class="ghost" @click=${this._refresh} ?disabled=${this._loadingSummary || this._loadingJobs}>
            ${icon.refresh()} Refresh
          </button>
        </div>
      </div>
      ${this._preset === "custom"
        ? html`
            <form class="filters" @submit=${this._applyCustom}>
              <div class="field">
                <label for="usage-since">From</label>
                <input
                  id="usage-since"
                  type="datetime-local"
                  .value=${this._customSince}
                  @input=${(e) => (this._customSince = e.target.value)}
                  required
                />
              </div>
              <div class="field">
                <label for="usage-until">To</label>
                <input
                  id="usage-until"
                  type="datetime-local"
                  .value=${this._customUntil}
                  @input=${(e) => (this._customUntil = e.target.value)}
                />
              </div>
              <button type="submit" class="primary">Apply</button>
            </form>
          `
        : null}
      <p class="muted small">
        ${this._since
          ? html`${formatDateTime(this._since)} → ${formatDateTime(this._until)}`
          : "—"}
      </p>
    `;
  }

  _renderSummary() {
    const t = this._summary?.totals;
    if (!t) {
      return html`<div class="summary-strip">
        <div class="stat"><div class="label">Fleet</div><div class="value muted">${this._loadingSummary ? "Loading…" : "—"}</div></div>
      </div>`;
    }
    const heldNonZero = (toWei(t.held_wei) ?? 0n) > 0n;
    return html`
      <div class="summary-strip">
        <div class="stat accent">
          <div class="label">Billed</div>
          <div class="value">${eth(t.billed_wei)}</div>
        </div>
        <div class="stat ${heldNonZero ? "warn" : ""}">
          <div class="label">Held</div>
          <div class="value">${eth(t.held_wei)}</div>
        </div>
        <div class="stat">
          <div class="label">Refunded</div>
          <div class="value">${eth(t.refunded_wei)}</div>
        </div>
        <div class="stat">
          <div class="label">Jobs</div>
          <div class="value num">${formatCount(t.jobs)}</div>
        </div>
        <div class="stat">
          <div class="label">Open</div>
          <div class="value num">${formatCount(t.open_jobs)}</div>
        </div>
        <div class="stat">
          <div class="label">Closed</div>
          <div class="value num">${formatCount(t.closed_jobs)}</div>
        </div>
      </div>
    `;
  }

  _renderChart() {
    if (!this._summary) return null;
    return html`
      <div class="card">
        <div class="card-head"><h3>Billed per day</h3></div>
        <cc-usage-chart
          .byDay=${this._summary.by_day || []}
          .start=${this._summary.since || this._since}
          .end=${this._summary.until || this._until}
        ></cc-usage-chart>
      </div>
    `;
  }

  _renderByUser() {
    const rows = this._usersSorted();
    return html`
      <div class="card">
        <div class="card-head">
          <h3>By user</h3>
          <span class="muted small">sorted by billed</span>
        </div>
        ${rows.length === 0
          ? html`<p class="empty">${this._loadingSummary ? "Loading…" : "No billed work in this window."}</p>`
          : html`
              <div class="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>User</th>
                      <th class="num">Jobs</th>
                      <th class="num">Billed (ETH)</th>
                      <th class="num">Held</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    ${rows.map((r) => {
                      const held = toWei(r.held_wei) ?? 0n;
                      const active = this._fUser?.user_id === r.user_id;
                      return html`
                        <tr>
                          <td title=${r.user_id}>
                            ${r.email || html`<span class="mono small">${r.user_id}</span>`}
                          </td>
                          <td class="num">${formatCount(r.jobs)}</td>
                          <td class="num">${eth(r.billed_wei)}</td>
                          <td class="num">${eth(r.held_wei, held > 0n ? "warn-text" : "muted")}</td>
                          <td class="actions">
                            <div class="row">
                              <button
                                class=${active ? "primary" : "ghost"}
                                @click=${() => this._filterByUser(active ? null : r)}
                              >
                                ${active ? "Showing jobs" : "Jobs"}
                              </button>
                            </div>
                          </td>
                        </tr>
                      `;
                    })}
                  </tbody>
                </table>
              </div>
            `}
      </div>
    `;
  }

  _renderByOffering() {
    const rows = this._summary?.by_offering || [];
    if (rows.length === 0) return null;
    return html`
      <div class="card">
        <div class="card-head"><h3>By offering</h3></div>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Capability</th>
                <th>Protocol</th>
                <th class="num">Jobs</th>
                <th class="num">Units</th>
                <th class="num">Billed (ETH)</th>
                <th class="num">Held</th>
              </tr>
            </thead>
            <tbody>
              ${rows.map(
                (o) => html`
                  <tr>
                    <td><span class="cap-path">${o.capability}<span class="off">/${o.offering}</span></span></td>
                    <td class="muted">${o.protocol || "—"}</td>
                    <td class="num">${formatCount(o.jobs)}</td>
                    <td class="num">${formatCount(o.units)} <span class="muted small">${o.work_unit || ""}</span></td>
                    <td class="num">${eth(o.billed_wei)}</td>
                    <td class="num">${eth(o.held_wei, (toWei(o.held_wei) ?? 0n) > 0n ? "warn-text" : "muted")}</td>
                  </tr>
                `,
              )}
            </tbody>
          </table>
        </div>
      </div>
    `;
  }

  _renderJobs() {
    const visible = this._visibleJobs();
    const total = this._jobs.total || 0;
    const from = total === 0 ? 0 : this._offset + 1;
    const to = Math.min(total, this._offset + (this._jobs.items?.length || 0));
    const clientFiltered = this._fEmail.trim() || this._fOutcome;
    return html`
      <div class="card" id="jobs">
        <div class="card-head">
          <h3>Jobs</h3>
          ${this._fUser
            ? html`<span class="pill info">
                ${this._fUser.email || this._fUser.user_id}
                <button
                  class="close"
                  style="margin-left: 6px; display: inline-flex; color: inherit;"
                  title="Clear user filter"
                  aria-label="Clear user filter"
                  @click=${() => this._filterByUser(null)}
                >
                  ${icon.x()}
                </button>
              </span>`
            : null}
        </div>
        <div class="filters">
          <div class="field grow">
            <label for="f-email">User email contains</label>
            <input
              id="f-email"
              type="text"
              placeholder="this page only"
              .value=${this._fEmail}
              @input=${(e) => (this._fEmail = e.target.value)}
            />
          </div>
          <div class="field">
            <label for="f-capability">Capability</label>
            <select
              id="f-capability"
              .value=${this._fCapability}
              @change=${(e) => this._setServerFilter("_fCapability", e.target.value)}
            >
              <option value="">all</option>
              ${this._capabilities().map(
                (c) => html`<option value=${c} ?selected=${c === this._fCapability}>${c}</option>`,
              )}
            </select>
          </div>
          <div class="field">
            <label for="f-state">State</label>
            <select
              id="f-state"
              .value=${this._fState}
              @change=${(e) => this._setServerFilter("_fState", e.target.value)}
            >
              <option value="">all</option>
              ${STATES.map((s) => html`<option value=${s} ?selected=${s === this._fState}>${s}</option>`)}
            </select>
          </div>
          <div class="field">
            <label for="f-outcome">Outcome</label>
            <select
              id="f-outcome"
              .value=${this._fOutcome}
              @change=${(e) => (this._fOutcome = e.target.value)}
            >
              <option value="">all</option>
              ${OUTCOMES.map(
                (o) => html`<option value=${o.key} ?selected=${o.key === this._fOutcome}>${o.label}</option>`,
              )}
            </select>
          </div>
        </div>
        <cc-usage-jobs-table
          .items=${visible}
          show-user
          ?loading=${this._loadingJobs}
          empty-text=${clientFiltered && (this._jobs.items?.length || 0) > 0
            ? "No jobs on this page match the email/outcome filter."
            : "No jobs in this window."}
        ></cc-usage-jobs-table>
        <div class="pager">
          <span>
            ${total === 0 ? "0 jobs" : `${from}–${to} of ${formatCount(total)}`}
            ${clientFiltered && visible.length !== (this._jobs.items?.length || 0)
              ? html` · <span class="muted">${visible.length} shown after filter</span>`
              : null}
          </span>
          <div class="row">
            <button class="ghost" ?disabled=${this._offset === 0 || this._loadingJobs} @click=${() => this._page(-1)}>
              Previous
            </button>
            <button class="ghost" ?disabled=${to >= total || this._loadingJobs} @click=${() => this._page(1)}>
              Next
            </button>
          </div>
        </div>
      </div>
    `;
  }

  render() {
    return html`
      ${this._renderPeriod()}
      ${this._error ? html`<div class="msg error mb-2">${this._error}</div>` : null}
      ${this._renderSummary()}
      ${this._renderChart()}
      ${this._renderByUser()}
      ${this._renderByOffering()}
      ${this._renderJobs()}
    `;
  }
}
customElements.define("cc-usage-admin", CcUsageAdmin);
