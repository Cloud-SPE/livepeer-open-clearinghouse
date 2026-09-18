import { LitElement, html } from "lit";
import * as api from "/admin/lib/api.js";
import { icon } from "/admin/lib/icons.js";
import {
  eth,
  formatCount,
  formatDateTime,
  formatDuration,
  formatEth,
  formatWei,
  timeAgo,
  toWei,
} from "/admin/lib/format.js";

const SPENT_DAYS = 30;
const DRAWER_JOBS = 50;

export class CcUsers extends LitElement {
  static properties = {
    _users: { state: true },
    _loading: { state: true },
    _error: { state: true },
    _busy: { state: true },
    _topupTarget: { state: true },
    _topupAmount: { state: true },
    _topupKind: { state: true },
    _configTarget: { state: true },
    _configForm: { state: true },
    _configEffective: { state: true },
    _usageByUser: { state: true },
    _usageError: { state: true },
    _lastActivity: { state: true },
    _drawerUser: { state: true },
    _drawerOverview: { state: true },
    _drawerJobs: { state: true },
    _drawerLoading: { state: true },
    _drawerError: { state: true },
    _resolveTarget: { state: true },
  };

  constructor() {
    super();
    this._users = [];
    this._loading = true;
    this._error = null;
    this._busy = false;
    this._topupTarget = null;
    this._topupAmount = "";
    this._topupKind = "manual";
    this._configTarget = null;
    this._configForm = null;
    this._configEffective = null;
    this._usageByUser = new Map(); // user_id -> { billed_wei, held_wei, jobs }
    this._usageError = null;
    this._lastActivity = new Map(); // user_id -> opened_at of most recent job
    this._drawerUser = null;
    this._drawerOverview = null;
    this._drawerJobs = null;
    this._drawerLoading = false;
    this._drawerError = null;
    this._resolveTarget = null;
  }

  createRenderRoot() {
    return this;
  }

  connectedCallback() {
    super.connectedCallback();
    this._onKey = (e) => {
      // The resolve dialog owns Escape while it is open.
      if (e.key === "Escape" && this._drawerUser && !this._resolveTarget) this._closeDrawer();
    };
    window.addEventListener("keydown", this._onKey);
    this._refresh();
  }

  disconnectedCallback() {
    window.removeEventListener("keydown", this._onKey);
    super.disconnectedCallback();
  }

  async _refresh() {
    this._loading = true;
    this._error = null;
    this._usageError = null;
    const until = new Date();
    const since = new Date(until.getTime() - SPENT_DAYS * 24 * 3600 * 1000);
    // Roster and usage load independently: a usage-API outage still leaves
    // the roster (and its approve/top-up actions) usable.
    const [roster, usage] = await Promise.allSettled([
      api.listUsers(100, 0),
      api.getFleetUsageSummary({ since: since.toISOString(), until: until.toISOString() }),
    ]);
    if (roster.status === "fulfilled") this._users = roster.value.items;
    else this._error = roster.reason?.message || String(roster.reason);
    if (usage.status === "fulfilled") {
      const map = new Map();
      for (const row of usage.value.by_user || []) map.set(row.user_id, row);
      this._usageByUser = map;
    } else {
      this._usageError = usage.reason?.message || String(usage.reason);
    }
    this._loading = false;
  }

  // --- usage drawer ---------------------------------------------------------

  async _openDrawer(user) {
    this._drawerUser = user;
    this._drawerOverview = null;
    this._drawerJobs = null;
    this._drawerError = null;
    this._drawerLoading = true;
    const [ov, jobs] = await Promise.allSettled([
      api.getUserUsageOverview(user.id),
      api.listUserUsageJobs(user.id, { limit: DRAWER_JOBS, offset: 0 }),
    ]);
    if (this._drawerUser?.id !== user.id) return; // closed or switched meanwhile
    if (ov.status === "fulfilled") this._drawerOverview = ov.value;
    if (jobs.status === "fulfilled") {
      this._drawerJobs = jobs.value;
      const latest = (jobs.value.items || []).reduce(
        (best, j) => (j.opened_at && (!best || j.opened_at > best) ? j.opened_at : best),
        null,
      );
      const next = new Map(this._lastActivity);
      next.set(user.id, latest || "none");
      this._lastActivity = next;
    }
    const failed = [ov, jobs].filter((r) => r.status === "rejected");
    if (failed.length) {
      this._drawerError = failed.map((r) => r.reason?.message || String(r.reason)).join(" · ");
    }
    this._drawerLoading = false;
  }

  _onResolveRequest(ev) {
    // Per-user rows omit user_id/user_email; fill them from the drawer's
    // user once here so the dialog gets a stable object across re-renders.
    const user = this._drawerUser;
    this._resolveTarget = {
      ...ev.detail.job,
      user_email: ev.detail.job.user_email || user?.email || null,
      user_id: ev.detail.job.user_id || user?.id || null,
    };
  }

  _onJobResolved() {
    // Held funds moved on the user's balance and the row closed: reload the
    // drawer in place and the roster behind it.
    if (this._drawerUser) this._openDrawer(this._drawerUser);
    this._refresh();
  }

  _closeDrawer() {
    this._resolveTarget = null;
    this._drawerUser = null;
    this._drawerOverview = null;
    this._drawerJobs = null;
    this._drawerError = null;
    this._drawerLoading = false;
  }

  _renderDrawer() {
    const user = this._drawerUser;
    if (!user) return null;
    const ov = this._drawerOverview;
    const period = ov?.period;
    const heldNonZero = (toWei(ov?.held_wei) ?? 0n) > 0n;
    // Derive the cap percentage from the two wei figures (exact, BigInt) and
    // only fall back to the API's pct_used, whose scale (0..1 vs 0..100) the
    // contract leaves open.
    const cap = toWei(period?.cap_wei);
    const spent = toWei(ov?.spent_period_wei) ?? 0n;
    const capPct =
      cap != null && cap > 0n
        ? Number((spent * 10000n) / cap) / 100
        : period?.pct_used != null
          ? Number(period.pct_used) <= 1
            ? Number(period.pct_used) * 100
            : Number(period.pct_used)
          : null;
    const overCap = capPct != null && capPct >= 100;
    return html`
      <div class="drawer-backdrop" @click=${this._closeDrawer}></div>
      <aside class="drawer" role="dialog" aria-modal="true" aria-label="Usage for ${user.email}">
        <div class="drawer-head">
          <div>
            <h2>${user.email}</h2>
            <div class="muted small mono">${user.id}</div>
          </div>
          <button class="close" aria-label="Close" @click=${this._closeDrawer}>${icon.x()}</button>
        </div>
        <div class="drawer-body">
          ${this._drawerError ? html`<div class="msg error mb-2">${this._drawerError}</div>` : null}
          ${ov
            ? html`
                <div class="summary-strip">
                  <div class="stat accent">
                    <div class="label">Available</div>
                    <div class="value">${eth(ov.available_wei)}</div>
                  </div>
                  <div class="stat ${heldNonZero ? "warn" : ""}">
                    <div class="label">Held</div>
                    <div class="value">${eth(ov.held_wei)}</div>
                  </div>
                  <div class="stat ${overCap ? "bad" : ""}">
                    <div class="label">Spent this period</div>
                    <div class="value">${eth(ov.spent_period_wei)}</div>
                    <div class="muted small">
                      ${period?.cap_wei != null
                        ? html`of ${formatEth(period.cap_wei)} cap · ${capPct == null ? "—" : `${capPct.toFixed(capPct < 10 ? 1 : 0)}%`}`
                        : "no cap"}
                      ${period?.seconds ? html` · ${formatDuration(period.seconds)} window` : null}
                    </div>
                  </div>
                  <div class="stat">
                    <div class="label">Spent (30d)</div>
                    <div class="value">${eth(ov.spent_30d_wei)}</div>
                  </div>
                  <div class="stat ${Number(ov.open_jobs) > 0 ? "warn" : ""}">
                    <div class="label">Open jobs</div>
                    <div class="value num">${formatCount(ov.open_jobs)}</div>
                  </div>
                </div>
                ${period
                  ? html`<p class="muted small">
                      Period ${formatDateTime(period.start)} → ${formatDateTime(period.end)}
                    </p>`
                  : null}
                ${(ov.by_day || []).length
                  ? html`<div class="card card-tight">
                      <cc-usage-chart .byDay=${ov.by_day} .start=${null} .end=${null}></cc-usage-chart>
                    </div>`
                  : null}
              `
            : this._drawerLoading
              ? html`<p class="muted">Loading overview…</p>`
              : null}
          <div class="card-head">
            <h3>Last ${DRAWER_JOBS} jobs</h3>
            ${this._drawerJobs
              ? html`<span class="muted small">
                  ${formatCount(Math.min(DRAWER_JOBS, this._drawerJobs.items?.length || 0))} of ${formatCount(this._drawerJobs.total)}
                </span>`
              : null}
          </div>
          <cc-usage-jobs-table
            .items=${this._drawerJobs?.items || []}
            ?loading=${this._drawerLoading}
            empty-text="No jobs recorded for this user."
            @cc-resolve-request=${this._onResolveRequest}
          ></cc-usage-jobs-table>
        </div>
      </aside>
      ${this._resolveTarget
        ? html`<cc-resolve-job
            .job=${this._resolveTarget}
            @cc-job-resolved=${this._onJobResolved}
            @cc-resolve-close=${() => (this._resolveTarget = null)}
          ></cc-resolve-job>`
        : null}
    `;
  }

  _renderLastActivity(user) {
    const v = this._lastActivity.get(user.id);
    if (!v) return html`<span class="muted" title="Open Usage to load">—</span>`;
    if (v === "none") return html`<span class="muted">never</span>`;
    return html`<span title=${v}>${timeAgo(v)}</span>`;
  }

  async _approve(id) {
    this._busy = true;
    this._error = null;
    try {
      await api.approveUser(id);
      await this._refresh();
    } catch (err) {
      this._error = err.message;
    } finally {
      this._busy = false;
    }
  }

  async _approveUnverified(user) {
    const ok = confirm(
      `Approve ${user.email} WITHOUT email verification?\n\n` +
        "The user will be able to log in immediately, but you can't " +
        "prove they control this email address. Only do this for users " +
        "you onboarded out-of-band.",
    );
    if (!ok) return;
    await this._approve(user.id);
  }

  async _resendVerification(user) {
    this._busy = true;
    this._error = null;
    try {
      await api.resendVerification(user.id);
    } catch (err) {
      this._error = err.message;
    } finally {
      this._busy = false;
    }
  }

  _openTopup(user) {
    this._topupTarget = user;
    this._topupAmount = "";
    this._topupKind = "manual";
  }

  _closeTopup() {
    this._topupTarget = null;
  }

  async _submitTopup(ev) {
    ev.preventDefault();
    const amount = parseInt(this._topupAmount, 10);
    if (!amount || amount <= 0) {
      this._error = "Amount must be a positive integer (wei).";
      return;
    }
    this._busy = true;
    this._error = null;
    try {
      await api.topupUser(this._topupTarget.id, amount, this._topupKind);
      this._topupTarget = null;
      await this._refresh();
    } catch (err) {
      this._error = err.message;
    } finally {
      this._busy = false;
    }
  }

  async _openConfig(user) {
    this._configTarget = user;
    this._configForm = null;
    this._configEffective = null;
    this._error = null;
    try {
      const res = await api.getBillingConfig(user.id);
      this._configForm = {
        spend_period_seconds: res.config.spend_period_seconds ?? "",
        spend_period_cap_wei: res.config.spend_period_cap_wei ?? "",
        auto_replenish_increment_wei:
          res.config.auto_replenish_increment_wei ?? "",
        auto_replenish_threshold_wei:
          res.config.auto_replenish_threshold_wei ?? "",
      };
      this._configEffective = res.effective;
    } catch (err) {
      this._error = err.message;
      this._configTarget = null;
    }
  }

  _closeConfig() {
    this._configTarget = null;
    this._configForm = null;
    this._configEffective = null;
  }

  _setConfigField(name, value) {
    this._configForm = { ...this._configForm, [name]: value };
  }

  async _submitConfig(ev) {
    ev.preventDefault();
    if (!this._configForm || !this._configTarget) return;
    const parse = (v) => {
      if (v === "" || v == null) return null;
      const n = parseInt(v, 10);
      return Number.isFinite(n) ? n : null;
    };
    const body = {
      spend_period_seconds: parse(this._configForm.spend_period_seconds),
      spend_period_cap_wei: parse(this._configForm.spend_period_cap_wei),
      auto_replenish_increment_wei: parse(
        this._configForm.auto_replenish_increment_wei,
      ),
      auto_replenish_threshold_wei: parse(
        this._configForm.auto_replenish_threshold_wei,
      ),
    };
    this._busy = true;
    this._error = null;
    try {
      const res = await api.putBillingConfig(this._configTarget.id, body);
      this._configEffective = res.effective;
      // Keep the modal open so the operator sees the new "effective" values.
    } catch (err) {
      this._error = err.message;
    } finally {
      this._busy = false;
    }
  }

  _renderConfigModal() {
    if (!this._configTarget || !this._configForm) return null;
    const f = this._configForm;
    const eff = this._configEffective;
    const field = (label, name, hint) => html`
      <div class="field">
        <label for=${name}>${label}</label>
        <input
          id=${name}
          type="text"
          inputmode="numeric"
          placeholder="(inherit default)"
          .value=${f[name]}
          @input=${(e) => this._setConfigField(name, e.target.value)}
        />
        ${hint
          ? html`<p class="muted" style="font-size: 12px;">${hint}</p>`
          : null}
      </div>
    `;
    return html`
      <div
        style="position: fixed; inset: 0; background: rgba(0,0,0,0.65);
               display: grid; place-items: center; z-index: 10;"
        @click=${this._closeConfig}
      >
        <div
          class="card"
          style="min-width: 480px; max-width: 90vw;"
          @click=${(e) => e.stopPropagation()}
        >
          <h3>Billing config — ${this._configTarget.email}</h3>
          <p class="muted">
            Leave a field blank to inherit the operator-wide default.
          </p>
          <form class="form mt-2" @submit=${this._submitConfig}>
            ${field("Spend period (seconds)", "spend_period_seconds",
              eff ? `effective: ${eff.spend_period_seconds}s` : null)}
            ${field("Spend cap per period (wei)", "spend_period_cap_wei",
              eff ? `effective: ${eff.spend_period_cap_wei} wei` : null)}
            ${field("Auto-replenish increment (wei)", "auto_replenish_increment_wei",
              eff ? `effective: ${eff.auto_replenish_increment_wei} wei` : null)}
            ${field("Auto-replenish threshold (wei)", "auto_replenish_threshold_wei",
              eff ? `effective: ${eff.auto_replenish_threshold_wei} wei` : null)}
            ${this._error ? html`<div class="msg error">${this._error}</div>` : null}
            <div class="row" style="justify-content: flex-end;">
              <button
                type="button"
                class="ghost"
                @click=${this._closeConfig}
                ?disabled=${this._busy}
              >
                Close
              </button>
              <button type="submit" class="primary" ?disabled=${this._busy}>
                ${this._busy ? "Saving…" : "Save"}
              </button>
            </div>
          </form>
        </div>
      </div>
    `;
  }

  _renderTopupModal() {
    if (!this._topupTarget) return null;
    return html`
      <div
        class="modal-backdrop"
        style="position: fixed; inset: 0; background: rgba(0,0,0,0.65);
               display: grid; place-items: center; z-index: 10;"
        @click=${this._closeTopup}
      >
        <div
          class="card"
          style="min-width: 340px; max-width: 90vw;"
          @click=${(e) => e.stopPropagation()}
        >
          <h3>Top up ${this._topupTarget.email}</h3>
          <p class="muted">Current balance: ${formatWei(this._topupTarget.balance_wei)} wei</p>
          <form class="form mt-2" @submit=${this._submitTopup}>
            <div class="field">
              <label for="amount">Amount (wei)</label>
              <input
                id="amount"
                type="text"
                inputmode="numeric"
                .value=${this._topupAmount}
                @input=${(e) => (this._topupAmount = e.target.value)}
                required
              />
            </div>
            <div class="field">
              <label for="kind">Kind</label>
              <select
                id="kind"
                .value=${this._topupKind}
                @change=${(e) => (this._topupKind = e.target.value)}
                style="background: var(--surface-2); border: 1px solid var(--border);
                       border-radius: var(--radius); padding: 8px 10px; color: var(--fg);"
              >
                <option value="manual">manual</option>
                <option value="initial">initial</option>
              </select>
            </div>
            ${this._error ? html`<div class="msg error">${this._error}</div>` : null}
            <div class="row" style="justify-content: flex-end;">
              <button
                type="button"
                class="ghost"
                @click=${this._closeTopup}
                ?disabled=${this._busy}
              >
                Cancel
              </button>
              <button type="submit" class="primary" ?disabled=${this._busy}>
                ${this._busy ? "Topping up…" : "Top up"}
              </button>
            </div>
          </form>
        </div>
      </div>
    `;
  }

  render() {
    return html`
      <div class="card">
        <h2>All users</h2>
        <p class="muted">${this._users.length} users · refresh to update</p>
        <div class="mt-2">
          <button class="ghost" @click=${this._refresh}>Refresh</button>
        </div>
      </div>

      ${this._error ? html`<div class="msg error mt-2">${this._error}</div>` : null}
      ${this._usageError
        ? html`<div class="msg warn mt-2">Usage columns unavailable: ${this._usageError}</div>`
        : null}

      <div class="card">
        ${this._loading
          ? html`<p class="muted">Loading…</p>`
          : this._users.length === 0
            ? html`<p class="muted">No users yet.</p>`
            : html`
                <div class="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Email</th>
                      <th>Verified</th>
                      <th>Approved</th>
                      <th class="num">Balance (ETH)</th>
                      <th class="num">Spent (${SPENT_DAYS}d)</th>
                      <th class="num">Held</th>
                      <th>Last activity</th>
                      <th>Signed up</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    ${this._users.map((u) => {
                      const usage = this._usageByUser.get(u.id);
                      const held = toWei(usage?.held_wei) ?? 0n;
                      return html`
                        <tr>
                          <td>${u.email}</td>
                          <td>
                            ${u.email_verified_at
                              ? html`<span class="pill">verified</span>`
                              : html`<span class="pill warn">pending</span>`}
                          </td>
                          <td>
                            ${u.approved
                              ? html`<span class="pill">approved</span>`
                              : html`<span class="pill warn">pending</span>`}
                          </td>
                          <td class="num">${eth(u.balance_wei)}</td>
                          <td class="num">
                            ${usage
                              ? eth(usage.billed_wei)
                              : html`<span class="muted" title=${this._usageError || "no billed work in window"}>—</span>`}
                          </td>
                          <td class="num">
                            ${usage
                              ? eth(usage.held_wei, held > 0n ? "warn-text" : "muted")
                              : html`<span class="muted">—</span>`}
                          </td>
                          <td>${this._renderLastActivity(u)}</td>
                          <td>${new Date(u.created_at).toLocaleDateString()}</td>
                          <td class="actions">
                            <div class="row">
                              <button
                                class="ghost"
                                title="Balances, held funds and the last ${DRAWER_JOBS} jobs"
                                ?disabled=${this._busy}
                                @click=${() => this._openDrawer(u)}
                              >
                                Usage
                              </button>
                              ${!u.approved && u.email_verified_at
                                ? html`<button
                                    class="primary"
                                    ?disabled=${this._busy}
                                    @click=${() => this._approve(u.id)}
                                  >
                                    Approve
                                  </button>`
                                : null}
                              ${!u.approved && !u.email_verified_at
                                ? html`<button
                                    class="warn"
                                    title="Approve without waiting for email verification — the user will be able to log in immediately"
                                    ?disabled=${this._busy}
                                    @click=${() => this._approveUnverified(u)}
                                  >
                                    Approve unverified
                                  </button>`
                                : null}
                              ${!u.email_verified_at
                                ? html`<button
                                    class="ghost"
                                    title="Send the user a fresh verification email"
                                    ?disabled=${this._busy}
                                    @click=${() => this._resendVerification(u)}
                                  >
                                    Resend verification
                                  </button>`
                                : null}
                              ${u.approved
                                ? html`<button
                                    class="ghost"
                                    ?disabled=${this._busy}
                                    @click=${() => this._openTopup(u)}
                                  >
                                    Top up
                                  </button>`
                                : null}
                              ${u.approved
                                ? html`<button
                                    class="ghost"
                                    ?disabled=${this._busy}
                                    @click=${() => this._openConfig(u)}
                                  >
                                    Settings
                                  </button>`
                                : null}
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

      ${this._renderTopupModal()}
      ${this._renderConfigModal()}
      ${this._renderDrawer()}
    `;
  }
}
customElements.define("cc-users", CcUsers);
