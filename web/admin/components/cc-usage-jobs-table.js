import { LitElement, html } from "lit";
import {
  eth,
  formatCount,
  formatDateTime,
  formatDuration,
  jobStatus,
  timeAgo,
} from "/admin/lib/format.js";

// One jobs table used by the Usage page (fleet, with a User column) and the
// per-user drawer in Users (without). Same columns everywhere so operators
// learn the layout once.
export class CcUsageJobsTable extends LitElement {
  static properties = {
    items: { attribute: false },
    showUser: { type: Boolean, attribute: "show-user" },
    loading: { type: Boolean },
    emptyText: { type: String, attribute: "empty-text" },
  };

  constructor() {
    super();
    this.items = [];
    this.showUser = false;
    this.loading = false;
    this.emptyText = "No jobs in this window.";
  }

  createRenderRoot() {
    return this;
  }

  _units(job) {
    const actual = job.actual_units;
    const max = job.max_total_units;
    const est = job.estimated_units;
    const unit = job.work_unit || "";
    if (actual != null) {
      return html`<span class="num" title="actual ${actual} · estimated ${est ?? "—"} · max ${max ?? "—"} ${unit}">${formatCount(actual)}</span>
        <span class="muted small">${unit}</span>`;
    }
    if (est != null || max != null) {
      return html`<span class="num muted" title="estimated ${est ?? "—"} · max ${max ?? "—"} ${unit}">~${formatCount(est ?? max)}</span>
        <span class="muted small">${unit}</span>`;
    }
    return html`<span class="muted">—</span>`;
  }

  _row(job) {
    const status = jobStatus(job);
    const held = job.held_wei;
    const heldNonZero = held != null && String(held) !== "0";
    return html`
      <tr>
        <td class="nowrap" title=${job.opened_at || ""}>
          ${formatDateTime(job.opened_at)}
          <div class="muted small">${timeAgo(job.opened_at)}</div>
        </td>
        ${this.showUser
          ? html`<td class="truncate" title=${job.user_email || job.user_id || ""}>
              ${job.user_email || html`<span class="mono small">${job.user_id}</span>`}
            </td>`
          : null}
        <td>
          <span class="cap-path">${job.capability}<span class="off">/${job.offering}</span></span>
          <div class="muted small">${job.protocol}</div>
        </td>
        <td class="truncate" title=${job.api_key_id || ""}>
          ${job.api_key_label || html`<span class="mono small">${job.api_key_id || "—"}</span>`}
        </td>
        <td>
          <span class="pill ${status.pill}" title="state: ${job.state} · outcome: ${job.accounting_outcome}">
            ${status.label}
          </span>
        </td>
        <td class="num">${this._units(job)}</td>
        <td class="num">${eth(job.billed_value_wei)}</td>
        <td class="num">${eth(held, heldNonZero ? "warn-text" : "muted")}</td>
        <td class="num">${eth(job.funded_value_wei, "muted")}</td>
        <td class="num">${eth(job.refunded_wei, "muted")}</td>
        <td class="num" title=${job.closed_at ? `closed ${formatDateTime(job.closed_at)}` : "still open"}>
          ${job.duration_seconds != null
            ? formatDuration(job.duration_seconds)
            : job.opened_at
              ? html`<span class="muted">${formatDuration((Date.now() - new Date(job.opened_at).getTime()) / 1000)}…</span>`
              : "—"}
        </td>
      </tr>
    `;
  }

  render() {
    if (this.loading && (!this.items || this.items.length === 0)) {
      return html`<p class="muted">Loading jobs…</p>`;
    }
    if (!this.items || this.items.length === 0) {
      return html`<p class="empty">${this.emptyText}</p>`;
    }
    return html`
      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Opened</th>
              ${this.showUser ? html`<th>User</th>` : null}
              <th>Capability</th>
              <th>API key</th>
              <th>State</th>
              <th class="num">Units</th>
              <th class="num">Billed (ETH)</th>
              <th class="num">Held</th>
              <th class="num">Funded</th>
              <th class="num">Refunded</th>
              <th class="num">Duration</th>
            </tr>
          </thead>
          <tbody>
            ${this.items.map((j) => this._row(j))}
          </tbody>
        </table>
      </div>
    `;
  }
}
customElements.define("cc-usage-jobs-table", CcUsageJobsTable);
