import { LitElement, html, svg } from "lit";
import * as api from "/portal/lib/api.js";
import {
  formatEth,
  formatWeiExact,
  formatDay,
  formatDateTime,
  formatInt,
  ledgerReasonLabel,
  percentOf,
  sumWei,
  toWei,
  weiToEthNumber,
} from "/portal/lib/format.js";

const SPARK_DAYS = 30;
const SPARK_W = 300;
const SPARK_H = 64;
const SPARK_PAD = 4;
const TOP_OFFERINGS = 5;

/** "YYYY-MM-DD" in UTC for a Date. Matches the API's by_day keys. */
function utcDay(date) {
  return date.toISOString().slice(0, 10);
}

/** Fill the last SPARK_DAYS days so days with no jobs plot as zero. */
function fillDays(byDay) {
  const byKey = new Map((byDay || []).map((d) => [d.day, d]));
  const out = [];
  const now = new Date();
  for (let i = SPARK_DAYS - 1; i >= 0; i--) {
    const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - i));
    const key = utcDay(d);
    const hit = byKey.get(key);
    out.push({
      day: key,
      jobs: hit ? Number(hit.jobs) || 0 : 0,
      billed_wei: hit ? hit.billed_wei : "0",
      eth: hit ? weiToEthNumber(hit.billed_wei) : 0,
    });
  }
  return out;
}

export class CcDashboard extends LitElement {
  static properties = {
    user: { type: Object },
    _balance: { state: true },
    _overview: { state: true },
    _summary: { state: true },
    _keys: { state: true },
    _ledger: { state: true },
    _loading: { state: true },
    _error: { state: true },
    _usageError: { state: true },
  };

  constructor() {
    super();
    this.user = null;
    this._balance = null;
    this._overview = null;
    this._summary = null;
    this._keys = [];
    this._ledger = [];
    this._loading = true;
    this._error = null;
    this._usageError = null;
  }

  createRenderRoot() {
    return this;
  }

  async connectedCallback() {
    super.connectedCallback();
    if (!this.user?.approved) {
      this._loading = false;
      return;
    }
    const since = new Date(Date.now() - SPARK_DAYS * 86400e3).toISOString();
    const [bal, keys, ledger, overview, summary] = await Promise.allSettled([
      api.getBalance(),
      api.listApiKeys(),
      api.getLedger(5),
      api.getUsageOverview(),
      api.getUsageSummary({ since }),
    ]);
    // Core account data: failure is a page error.
    const core = [bal, keys, ledger].find((r) => r.status === "rejected");
    if (core) this._error = core.reason?.message || String(core.reason);
    if (bal.status === "fulfilled") this._balance = bal.value;
    if (keys.status === "fulfilled") this._keys = keys.value.items || [];
    if (ledger.status === "fulfilled") this._ledger = ledger.value.items || [];
    // Usage data: degrade to the plain balance if unavailable.
    if (overview.status === "fulfilled") this._overview = overview.value;
    if (summary.status === "fulfilled") this._summary = summary.value;
    const usage = [overview, summary].find((r) => r.status === "rejected");
    if (usage) this._usageError = usage.reason?.message || String(usage.reason);
    this._loading = false;
  }

  _approvalPill() {
    if (this.user?.approved) {
      return html`<span class="pill ok">Approved</span>`;
    }
    if (!this.user?.email_verified_at) {
      return html`<span class="pill warn">Verify email</span>`;
    }
    return html`<span class="pill warn">Pending approval</span>`;
  }

  // ----- metrics -----

  _ethValue(wei, extraClass = "") {
    return html`<div class="value eth num ${extraClass}" title=${formatWeiExact(wei)}>
      ${formatEth(wei)}
    </div>`;
  }

  _renderPeriodMetric() {
    const ov = this._overview;
    const period = ov?.period || {};
    const cap = period.cap_wei;
    const pct = cap != null ? percentOf(ov.spent_period_wei, cap) : null;
    const tone = pct == null ? "" : pct >= 90 ? "bad" : pct >= 75 ? "warn" : "";
    const start = period.start ? formatDay(String(period.start).slice(0, 10)) : null;
    const end = period.end ? formatDay(String(period.end).slice(0, 10)) : null;
    const range = start && end ? `${start} – ${end}` : start ? `from ${start}` : null;
    return html`
      <div class="metric ${tone}">
        <div class="label">Spent this period</div>
        ${this._ethValue(ov?.spent_period_wei)}
        ${cap != null
          ? html`
              <div class="sub">
                of <span class="num" title=${formatWeiExact(cap)}>${formatEth(cap)}</span> cap
                (${pct == null ? "—" : `${pct.toFixed(pct < 10 ? 1 : 0)}%`})
              </div>
              <div
                class="progress ${tone}"
                role="progressbar"
                aria-label="Spend against period cap"
                aria-valuemin="0"
                aria-valuemax="100"
                aria-valuenow=${pct == null ? 0 : Math.min(100, pct)}
              >
                <span style="width: ${pct == null ? 0 : Math.min(100, pct)}%"></span>
              </div>
            `
          : html`<div class="sub">No spending cap${range ? ` · period ${range}` : ""}</div>`}
      </div>
    `;
  }

  _renderMetrics() {
    const ov = this._overview;
    const activeKeys = this._keys.filter((k) => !k.revoked_at).length;
    // Fall back to the plain balance if the overview endpoint is unavailable.
    const available = ov ? ov.available_wei : this._balance?.amount_wei;
    return html`
      <div class="metric-grid">
        <div class="metric accent eth-hero">
          <div class="label">Available</div>
          ${this._ethValue(available)}
          <div class="sub">
            ${ov
              ? html`Spent in the last 30 days:
                  <span class="num" title=${formatWeiExact(ov.spent_30d_wei)}>${formatEth(ov.spent_30d_wei)}</span>`
              : "Credit you can spend on new jobs."}
          </div>
        </div>
        <div class="metric info">
          <div class="label">Held by open jobs</div>
          ${this._ethValue(ov ? ov.held_wei : null)}
          <div class="sub">
            ${ov
              ? `${formatInt(ov.open_jobs)} open job${Number(ov.open_jobs) === 1 ? "" : "s"} — released when they settle`
              : "Funds reserved while jobs run."}
          </div>
        </div>
        ${this._renderPeriodMetric()}
        <div class="metric">
          <div class="label">Active API keys</div>
          <div class="value num">${activeKeys}</div>
          <div class="sub">${this._keys.length} total (incl. revoked)</div>
        </div>
      </div>
    `;
  }

  // ----- 30-day sparkline -----

  _renderSparkline() {
    const days = fillDays(this._overview?.by_day);
    const max = Math.max(0, ...days.map((d) => d.eth));
    const n = days.length;
    const stepX = SPARK_W / (n - 1);
    const innerH = SPARK_H - SPARK_PAD * 2;
    const y = (v) => SPARK_PAD + innerH - (max > 0 ? (v / max) * innerH : 0);
    const pts = days.map((d, i) => [i * stepX, y(d.eth)]);
    const line = pts.map(([px, py], i) => `${i === 0 ? "M" : "L"}${px.toFixed(1)},${py.toFixed(1)}`).join(" ");
    const area = `${line} L${SPARK_W},${SPARK_H - SPARK_PAD} L0,${SPARK_H - SPARK_PAD} Z`;
    const totalWei = sumWei(days.map((d) => d.billed_wei));
    const totalJobs = days.reduce((a, d) => a + d.jobs, 0);
    const peak = days.reduce((best, d) => (d.eth > (best?.eth ?? -1) ? d : best), null);
    const first = days[0];
    const last = days[n - 1];
    const slot = SPARK_W / n;

    return html`
      <div class="card">
        <div class="row" style="justify-content: space-between;">
          <h3 style="margin: 0;">Spend, last 30 days</h3>
          <a href="#/usage" class="small">Usage →</a>
        </div>
        <p class="muted small mt-1">
          <span class="num" title=${formatWeiExact(totalWei.toString())}>${formatEth(totalWei.toString())}</span>
          across ${formatInt(totalJobs)} job${totalJobs === 1 ? "" : "s"}${peak && peak.eth > 0
            ? html` · peak ${formatDay(peak.day)}
                (<span class="num" title=${formatWeiExact(peak.billed_wei)}>${formatEth(peak.billed_wei)}</span>)`
            : ""}
        </p>
        ${totalJobs === 0
          ? html`<p class="muted mt-1">No billed jobs in the last 30 days. Spend per day will chart here once jobs settle.</p>`
          : null}
        <svg
          class="sparkline mt-1"
          viewBox="0 0 ${SPARK_W} ${SPARK_H}"
          preserveAspectRatio="none"
          role="img"
          aria-label="Daily spend over the last 30 days"
        >
          ${svg`<path class="area" d=${area}></path>`}
          ${svg`<line class="baseline" x1="0" y1=${SPARK_H - SPARK_PAD} x2=${SPARK_W} y2=${SPARK_H - SPARK_PAD}></line>`}
          ${svg`<path class="line" d=${line}></path>`}
          ${days.map(
            (d, i) => svg`<rect class="hit" x=${i * slot} y="0" width=${slot} height=${SPARK_H}>
              <title>${formatDay(d.day)}: ${formatEth(d.billed_wei)} · ${d.jobs} job${d.jobs === 1 ? "" : "s"}</title>
            </rect>`,
          )}
        </svg>
        <div class="sparkline-labels">
          <span>
            ${formatDay(first.day)} ·
            <strong class="num" title=${formatWeiExact(first.billed_wei)}>${formatEth(first.billed_wei)}</strong>
          </span>
          <span>
            Today ·
            <strong class="num" title=${formatWeiExact(last.billed_wei)}>${formatEth(last.billed_wei)}</strong>
          </span>
        </div>
      </div>
    `;
  }

  // ----- top offerings -----

  _renderTopOfferings() {
    const rows = [...(this._summary?.by_offering || [])]
      .filter((r) => toWei(r.billed_wei) != null)
      .sort((a, b) => (toWei(b.billed_wei) > toWei(a.billed_wei) ? 1 : toWei(b.billed_wei) < toWei(a.billed_wei) ? -1 : 0))
      .slice(0, TOP_OFFERINGS);
    const total = sumWei(rows.map((r) => r.billed_wei));
    return html`
      <div class="card">
        <div class="row" style="justify-content: space-between;">
          <h3 style="margin: 0;">Top offerings, last 30 days</h3>
          <a href="#/usage" class="small">All jobs →</a>
        </div>
        ${rows.length === 0
          ? html`<p class="muted mt-1">Once jobs run, the capabilities and offerings you spend the most on will list here.</p>`
          : html`
              <ul class="rank-list mt-2">
                ${rows.map((r) => {
                  const pct = total > 0n ? percentOf(r.billed_wei, total.toString()) : 0;
                  return html`
                    <li>
                      <span class="name" title="${r.capability} / ${r.offering}">
                        ${r.capability}<span class="muted"> / ${r.offering}</span>
                      </span>
                      <span class="num" title=${formatWeiExact(r.billed_wei)}>
                        ${formatEth(r.billed_wei)}
                        <span class="muted small">· ${formatInt(r.jobs)} job${Number(r.jobs) === 1 ? "" : "s"}</span>
                      </span>
                      <div class="progress" aria-hidden="true"><span style="width: ${pct || 0}%"></span></div>
                    </li>
                  `;
                })}
              </ul>
            `}
      </div>
    `;
  }

  // ----- misc cards -----

  _renderApprovalCard() {
    if (this.user?.approved) return null;
    return html`
      <div class="card">
        <h3>Approval needed</h3>
        <p class="muted">
          An operator must approve your account before you can create API keys
          or call the network. You'll receive an email when that happens.
        </p>
      </div>
    `;
  }

  _renderRecent() {
    if (!this.user?.approved) return null;
    return html`
      <div class="card">
        <div class="row" style="justify-content: space-between;">
          <h3 style="margin: 0;">Recent activity</h3>
          <a href="#/activity" class="small">View all →</a>
        </div>
        ${this._loading
          ? html`<p class="muted mt-1">Loading…</p>`
          : this._ledger.length === 0
            ? html`<p class="muted mt-1">No activity yet.</p>`
            : html`
                <table class="mt-1">
                  <thead>
                    <tr>
                      <th>When</th>
                      <th>What</th>
                      <th class="right">Amount</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${this._ledger.map((e) => {
                      const w = toWei(e.delta_wei);
                      return html`
                        <tr>
                          <td class="nowrap">${formatDateTime(e.created_at)}</td>
                          <td>${ledgerReasonLabel(e.reason)}</td>
                          <td class="right num" title=${formatWeiExact(e.delta_wei)}>
                            ${w != null && w > 0n ? "+" : ""}${formatEth(e.delta_wei)}
                          </td>
                        </tr>
                      `;
                    })}
                  </tbody>
                </table>
              `}
      </div>
    `;
  }

  _renderNextSteps() {
    if (!this.user?.approved) return null;
    return html`
      <div class="card">
        <h3>Next steps</h3>
        <ul style="margin-left: 18px; color: var(--muted);">
          <li>
            <a href="#/api-keys">Manage your API keys</a> — create one per app
            you build.
          </li>
          <li>
            Discover capabilities via <code>GET /v1/capabilities</code>.
          </li>
          <li>
            Mint payments with <code>POST /v1/payments/mint</code> using your
            API key.
          </li>
        </ul>
      </div>
    `;
  }

  render() {
    if (!this.user) return null;
    return html`
      <div class="row mb-2" style="justify-content: space-between;">
        <h1 style="margin: 0;">Dashboard</h1>
        <span class="row small muted">${this.user.email} ${this._approvalPill()}</span>
      </div>
      ${this._error ? html`<div class="msg error mb-2">${this._error}</div>` : null}
      ${this._usageError && this.user.approved
        ? html`<div class="msg warn mb-2">Usage figures unavailable: ${this._usageError}</div>`
        : null}
      ${this.user.approved
        ? this._loading
          ? html`<p class="muted">Loading…</p>`
          : html`
              ${this._renderMetrics()}
              ${this._renderSparkline()}
              ${this._renderTopOfferings()}
            `
        : null}
      ${this._renderApprovalCard()}
      ${this._renderRecent()}
      ${this._renderNextSteps()}
    `;
  }
}
customElements.define("cc-dashboard", CcDashboard);
