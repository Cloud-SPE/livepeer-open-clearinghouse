import { LitElement, html } from "lit";
import * as api from "/admin/lib/api.js";
import { icon } from "/admin/lib/icons.js";
import {
  blockedReasonLabel,
  eth,
  formatCount,
  formatDateTime,
  formatDuration,
  formatEth,
  timeAgo,
  toWei,
} from "/admin/lib/format.js";

const FLEET_DAYS = 7;
const STALE_AFTER_SECONDS = 900;

export class CcOverview extends LitElement {
  static properties = {
    _users: { state: true },
    _pending: { state: true },
    _audit: { state: true },
    _deposits: { state: true },
    _loading: { state: true },
    _error: { state: true },
    _attention: { state: true },
    _fleet: { state: true },
    _fleetSince: { state: true },
    _fleetUntil: { state: true },
    _usageError: { state: true },
    _usageLoading: { state: true },
    _showUnresolved: { state: true },
    _showFailures: { state: true },
    _showZeroOutput: { state: true },
    _showUnterminated: { state: true },
    _resolveTarget: { state: true },
  };

  constructor() {
    super();
    this._users = { total: 0, items: [] };
    this._pending = { items: [] };
    this._audit = { items: [] };
    this._deposits = { items: [] };
    this._loading = true;
    this._error = null;
    this._attention = null;
    this._fleet = null;
    this._fleetSince = null;
    this._fleetUntil = null;
    this._usageError = null;
    this._usageLoading = true;
    this._showUnresolved = false;
    this._showFailures = false;
    this._showZeroOutput = false;
    this._showUnterminated = false;
    this._resolveTarget = null;
  }

  createRenderRoot() {
    return this;
  }

  connectedCallback() {
    super.connectedCallback();
    // The two loads are independent on purpose: a usage-API outage must not
    // blank the roster/deposit metrics, and vice versa.
    this._loadRoster();
    this._loadUsage();
  }

  async _loadRoster() {
    this._loading = true;
    try {
      const [u, p, a, d] = await Promise.all([
        api.listUsers(100, 0),
        api.listPending(),
        api.listAudit(5),
        api.listDepositSnapshots(1),
      ]);
      this._users = u;
      this._pending = p;
      this._audit = a;
      this._deposits = d;
    } catch (err) {
      this._error = err.message;
    } finally {
      this._loading = false;
    }
  }

  async _loadUsage() {
    this._usageLoading = true;
    this._usageError = null;
    const until = new Date();
    const since = new Date(until.getTime() - FLEET_DAYS * 24 * 3600 * 1000);
    this._fleetSince = since.toISOString();
    this._fleetUntil = until.toISOString();
    const results = await Promise.allSettled([
      api.getUsageAttention({ stale_after_seconds: STALE_AFTER_SECONDS }),
      api.getFleetUsageSummary({ since: this._fleetSince, until: this._fleetUntil }),
    ]);
    const [att, fleet] = results;
    if (att.status === "fulfilled") this._attention = att.value;
    if (fleet.status === "fulfilled") this._fleet = fleet.value;
    const failed = results.filter((r) => r.status === "rejected");
    if (failed.length) {
      this._usageError = failed.map((r) => r.reason?.message || String(r.reason)).join(" · ");
    }
    this._usageLoading = false;
  }

  // --- attention ----------------------------------------------------------

  _onJobResolved() {
    // Held funds moved: the attention counters and fleet held/billed totals
    // both change, and _loadUsage fetches exactly those two.
    this._loadUsage();
  }

  _renderAttention() {
    const a = this._attention;
    const counts = a?.counts || {};
    const unresolved = Number(counts.unresolved ?? (a?.unresolved?.length || 0));
    const failures = Number(counts.settlement_failures_24h ?? (a?.settlement_failures?.length || 0));
    const zeroOutput = Number(counts.zero_output_sessions ?? (a?.zero_output_sessions?.length || 0));
    const unterminated = Number(counts.unterminated_sessions ?? (a?.unterminated_sessions?.length || 0));
    const heldTotal = (a?.unresolved || []).reduce((n, j) => n + (toWei(j.funded_value_wei) ?? 0n), 0n);
    const loading = this._usageLoading && !a;

    return html`
      <div class="section-title">Attention</div>
      <div class="attention-grid">
        <button
          type="button"
          class="metric ${unresolved > 0 ? "alert" : "quiet"}"
          aria-expanded=${this._showUnresolved}
          aria-controls="attention-unresolved"
          @click=${() => (this._showUnresolved = !this._showUnresolved)}
        >
          <span class="chevron">${icon.chevron()}</span>
          <div class="label">Unresolved jobs holding funds</div>
          <div class="value num">${loading ? "…" : formatCount(unresolved)}</div>
          <div class="sub">
            ${loading
              ? "Loading…"
              : unresolved > 0
                ? html`${formatEth(heldTotal)} ETH funded and not settled · stale after ${formatDuration(STALE_AFTER_SECONDS)}`
                : "Nothing stuck"}
          </div>
        </button>
        <button
          type="button"
          class="metric ${failures > 0 ? "alert-warn" : "quiet"}"
          aria-expanded=${this._showFailures}
          aria-controls="attention-failures"
          @click=${() => (this._showFailures = !this._showFailures)}
        >
          <span class="chevron">${icon.chevron()}</span>
          <div class="label">Settlement failures (24h)</div>
          <div class="value num">${loading ? "…" : formatCount(failures)}</div>
          <div class="sub">
            ${loading ? "Loading…" : failures > 0 ? "Broker settle calls that errored" : "No failures"}
          </div>
        </button>
        <button
          type="button"
          class="metric ${zeroOutput > 0 ? "alert-warn" : "quiet"}"
          aria-expanded=${this._showZeroOutput}
          aria-controls="attention-zero-output"
          @click=${() => (this._showZeroOutput = !this._showZeroOutput)}
        >
          <span class="chevron">${icon.chevron()}</span>
          <div class="label">Zero-output sessions (24h)</div>
          <div class="value num">${loading ? "…" : formatCount(zeroOutput)}</div>
          <div class="sub">${loading ? "Loading…" : zeroOutput > 0 ? "Closed after 60s with no metered work" : "No zero-output sessions"}</div>
        </button>
        <button
          type="button"
          class="metric ${unterminated > 0 ? "alert" : "quiet"}"
          aria-expanded=${this._showUnterminated}
          aria-controls="attention-unterminated"
          @click=${() => (this._showUnterminated = !this._showUnterminated)}
        >
          <span class="chevron">${icon.chevron()}</span>
          <div class="label">Sessions requiring review</div>
          <div class="value num">${loading ? "…" : formatCount(unterminated)}</div>
          <div class="sub">${loading ? "Loading…" : unterminated > 0 ? "No terminal broker record after the review threshold" : "Nothing stuck"}</div>
        </button>
      </div>
      ${this._showUnresolved ? this._renderUnresolvedList() : null}
      ${this._showFailures ? this._renderFailuresList() : null}
      ${this._showZeroOutput ? this._renderZeroOutputList() : null}
      ${this._showUnterminated ? this._renderUnterminatedList() : null}
      ${this._resolveTarget
        ? html`<cc-resolve-job
            .job=${this._resolveTarget}
            @cc-job-resolved=${this._onJobResolved}
            @cc-resolve-close=${() => (this._resolveTarget = null)}
          ></cc-resolve-job>`
        : null}
    `;
  }

  _renderUnresolvedList() {
    const rows = this._attention?.unresolved || [];
    return html`
      <div class="card" id="attention-unresolved">
        <div class="card-head">
          <h3>Unresolved jobs</h3>
          <span class="muted small">funds stay held until the broker settles or the job is force-closed</span>
        </div>
        ${rows.length === 0
          ? html`<p class="empty">No unresolved jobs.</p>`
          : html`
              <div class="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>User</th>
                      <th>Capability</th>
                      <th class="num">Funded (ETH)</th>
                      <th class="num">Age</th>
                      <th>Opened</th>
                      <th>Job</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    ${rows.map(
                      (j) => html`
                        <tr>
                          <td title=${j.user_id}>${j.user_email || html`<span class="mono small">${j.user_id}</span>`}</td>
                          <td>
                            <span class="cap-path">${j.capability}<span class="off">/${j.offering}</span></span>
                            <div class="muted small">
                              ${j.protocol}
                              ${j.blocked_reason
                                ? html` <span class="tag" title="reconciler stopped: ${j.blocked_reason}">${blockedReasonLabel(j.blocked_reason)}</span>`
                                : null}
                            </div>
                          </td>
                          <td class="num">${eth(j.funded_value_wei, "danger-text")}</td>
                          <td class="num danger-text">${formatDuration(j.age_seconds)}</td>
                          <td class="nowrap" title=${j.opened_at}>${formatDateTime(j.opened_at)}</td>
                          <td class="mono small truncate" title=${j.job_id}>${j.job_id}</td>
                          <td class="actions">
                            <button type="button" class="warn" @click=${() => (this._resolveTarget = j)}>Resolve</button>
                          </td>
                        </tr>
                      `,
                    )}
                  </tbody>
                </table>
              </div>
            `}
      </div>
    `;
  }

  _renderFailuresList() {
    const rows = this._attention?.settlement_failures || [];
    return html`
      <div class="card" id="attention-failures">
        <div class="card-head"><h3>Settlement failures (24h)</h3></div>
        ${rows.length === 0
          ? html`<p class="empty">No settlement failures in the last 24 hours.</p>`
          : html`
              <div class="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Time</th>
                      <th>User</th>
                      <th>Protocol</th>
                      <th>Code</th>
                      <th>Reason</th>
                      <th>Session</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${rows.map(
                      (f) => html`
                        <tr>
                          <td class="nowrap" title=${f.at}>
                            ${formatDateTime(f.at)}
                            <div class="muted small">${timeAgo(f.at)}</div>
                          </td>
                          <td title=${f.user_id}>${f.user_email || html`<span class="mono small">${f.user_id}</span>`}</td>
                          <td class="muted">${f.protocol || "—"}</td>
                          <td><span class="pill warn">${f.code || "unknown"}</span></td>
                          <td style="max-width: 420px; word-break: break-word;">${f.reason || "—"}</td>
                          <td class="mono small truncate" title=${f.session_id}>${f.session_id || "—"}</td>
                        </tr>
                      `,
                    )}
                  </tbody>
                </table>
              </div>
            `}
      </div>
    `;
  }

  _renderZeroOutputList() {
    const rows = this._attention?.zero_output_sessions || [];
    return html`
      <div class="card" id="attention-zero-output">
        <div class="card-head"><h3>Zero-output sessions (24h)</h3></div>
        ${rows.length === 0
          ? html`<p class="empty">No long-running or output-failed sessions closed with zero units.</p>`
          : html`
              <div class="table-scroll">
                <table>
                  <thead><tr><th>User</th><th>Capability</th><th>Diagnosis</th><th>Failure</th><th class="num">Duration</th><th>Closed</th><th>Broker session</th></tr></thead>
                  <tbody>
                    ${rows.map((s) => html`
                      <tr>
                        <td title=${s.user_id}>${s.user_email || html`<span class="mono small">${s.user_id}</span>`}</td>
                        <td><span class="cap-path">${s.capability}<span class="off">/${s.offering}</span></span></td>
                        <td><span class="pill ${s.termination_reason === "output_failed" ? "bad" : "warn"}">${s.termination_reason || s.output_state || "zero output"}</span></td>
                        <td class="mono small">${s.last_failure_code || "—"}</td>
                        <td class="num warn-text">${formatDuration(s.duration_seconds)}</td>
                        <td class="nowrap" title=${s.closed_at}>${formatDateTime(s.closed_at)}</td>
                        <td class="mono small truncate" title=${s.broker_session_id || ""}>${s.broker_session_id || "—"}</td>
                      </tr>
                    `)}
                  </tbody>
                </table>
              </div>`}
      </div>
    `;
  }

  _renderUnterminatedList() {
    const rows = this._attention?.unterminated_sessions || [];
    return html`
      <div class="card" id="attention-unterminated">
        <div class="card-head">
          <h3>Sessions requiring review</h3>
          <span class="muted small">time alone never releases the customer hold</span>
        </div>
        ${rows.length === 0
          ? html`<p class="empty">No sessions are beyond the operator review threshold.</p>`
          : html`
              <div class="table-scroll">
                <table>
                  <thead><tr><th>User</th><th>Capability</th><th class="num">Overdue</th><th>Last funded</th><th>Broker session</th></tr></thead>
                  <tbody>
                    ${rows.map((s) => html`
                      <tr>
                        <td title=${s.user_id}>${s.user_email || html`<span class="mono small">${s.user_id}</span>`}</td>
                        <td><span class="cap-path">${s.capability}<span class="off">/${s.offering}</span></span></td>
                        <td class="num danger-text">${formatDuration(s.overdue_seconds)}</td>
                        <td class="nowrap" title=${s.last_funded_at}>${formatDateTime(s.last_funded_at)}</td>
                        <td class="mono small truncate" title=${s.broker_session_id || ""}>${s.broker_session_id || "—"}</td>
                      </tr>
                    `)}
                  </tbody>
                </table>
              </div>`}
      </div>
    `;
  }

  // --- fleet ---------------------------------------------------------------

  _renderFleet() {
    const f = this._fleet;
    const t = f?.totals;
    const byOffering = f?.by_offering || [];
    return html`
      <div class="section-title">Fleet · last ${FLEET_DAYS} days</div>
      <div class="card">
        <div class="card-head">
          <h3>Billed per day</h3>
          ${t
            ? html`<span class="muted small">
                ${formatCount(t.jobs)} jobs · ${formatEth(t.billed_wei)} ETH billed ·
                <span class=${(toWei(t.held_wei) ?? 0n) > 0n ? "warn-text" : ""}>${formatEth(t.held_wei)} ETH held</span>
              </span>`
            : null}
        </div>
        ${f
          ? html`<cc-usage-chart
              .byDay=${f.by_day || []}
              .start=${f.since || this._fleetSince}
              .end=${f.until || this._fleetUntil}
            ></cc-usage-chart>`
          : html`<p class="empty">${this._usageLoading ? "Loading…" : "Fleet usage unavailable."}</p>`}
      </div>
      <div class="card">
        <div class="card-head"><h3>By offering</h3></div>
        ${byOffering.length === 0
          ? html`<p class="empty">${this._usageLoading ? "Loading…" : "No billed work in the last 7 days."}</p>`
          : html`
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
                    ${byOffering.map(
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
            `}
      </div>
    `;
  }

  // --- roster (pre-existing) ----------------------------------------------

  _renderMetrics() {
    const approved = this._users.items.filter((u) => u.approved).length;
    const latestDeposit = this._deposits.items[0];
    return html`
      <div class="metric-grid">
        <div class="metric accent">
          <div class="label">Total users</div>
          <div class="value">${this._users.total}</div>
          <div class="sub">${approved} approved</div>
        </div>
        <div class="metric warn">
          <div class="label">Pending approval</div>
          <div class="value">${this._pending.items.length}</div>
          <div class="sub">Awaiting operator action</div>
        </div>
        <div class="metric info">
          <div class="label">Pooled wallet deposit (ETH)</div>
          <div class="value mono" style="font-size: 18px;">
            ${formatEth(latestDeposit?.deposit_wei)}
          </div>
          <div class="sub">
            ${latestDeposit
              ? `Snapshot ${timeAgo(latestDeposit.taken_at)}`
              : "No snapshot yet"}
          </div>
        </div>
        <div class="metric">
          <div class="label">Last audit entry</div>
          <div class="value" style="font-size: 14px; font-weight: 500;">
            ${this._audit.items[0]
              ? html`<code>${this._audit.items[0].action}</code>`
              : html`<span class="muted">none</span>`}
          </div>
          <div class="sub">
            ${this._audit.items[0]
              ? `${timeAgo(this._audit.items[0].created_at)} · ${this._audit.items[0].operator_email}`
              : "—"}
          </div>
        </div>
      </div>
    `;
  }

  render() {
    return html`
      <div class="card-head">
        <h1 style="margin: 0;">Overview</h1>
        <button
          class="ghost"
          @click=${() => {
            this._loadRoster();
            this._loadUsage();
          }}
          ?disabled=${this._loading || this._usageLoading}
        >
          ${icon.refresh()} Refresh
        </button>
      </div>
      ${this._usageError
        ? html`<div class="msg warn mb-2">Usage API: ${this._usageError}</div>`
        : null}
      ${this._renderAttention()}
      ${this._renderFleet()}
      <div class="section-title">Accounts &amp; deposits</div>
      ${this._error ? html`<div class="msg error mb-2">${this._error}</div>` : null}
      ${this._loading ? html`<p class="muted">Loading…</p>` : this._renderMetrics()}
      <div class="card">
        <h3>What to do here</h3>
        <ul style="margin-left: 18px; color: var(--muted);">
          <li><strong>Users</strong> — approve pending users, top up balances, edit per-user spend caps, open a user's usage drawer.</li>
          <li><strong>Usage</strong> — fleet spend by user and offering, every job with its billing outcome.</li>
          <li><strong>Pending</strong> — focused triage of just the not-yet-approved accounts.</li>
          <li><strong>Audit log</strong> — every state-mutating operator action.</li>
          <li><strong>Deposits</strong> — pooled-wallet on-chain deposit snapshots over time.</li>
        </ul>
      </div>
    `;
  }
}
customElements.define("cc-overview", CcOverview);
