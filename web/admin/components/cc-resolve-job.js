import { LitElement, html } from "lit";
import * as api from "/admin/lib/api.js";
import { icon } from "/admin/lib/icons.js";
import { blockedReasonLabel, eth, formatCount, formatEth, toWei } from "/admin/lib/format.js";

// Operator recourse for a job the reconciler could not settle. Hosted by
// whichever view lists the row (overview attention list, fleet jobs table,
// per-user drawer). The host passes the row as `.job` and listens for:
//   cc-job-resolved  { job_id, result }  — after a 2xx, so it can refresh
//   cc-resolve-close                     — Cancel / Done / Escape / backdrop
//
// Rows come from three endpoints with slightly different shapes; `_job()`
// normalises them so the dialog body reads the same everywhere.

const ACTIONS = [
  {
    key: "refund_hold",
    label: "Refund the hold",
    explain: (j) =>
      `Release the full ${formatEth(j.funded)} ETH back to the user. Use when the work never happened.`,
  },
  {
    key: "accept_reported",
    label: "Accept broker-reported units",
    explain: (j) =>
      j.reported != null
        ? `Charge ${formatCount(j.reported)} ${j.unit} at the snapshot price and refund the rest. The fair outcome when the broker did the work but settlement could not be verified.`
        : "Charge whatever the broker reported at the snapshot price and refund the rest. Fails if the broker never reported units for this job.",
  },
  {
    key: "charge_full",
    label: "Charge the full hold",
    explain: (j) =>
      `Bill the entire ${formatEth(j.funded)} ETH; the user gets nothing back. The conservative choice when you cannot tell how much work was done.`,
  },
];

const REASON_TEXT = {
  already_closed: "Already closed — someone (or the reconciler) settled this job first. Refresh to see its outcome.",
  no_broker_report: "No broker report for this job; choose refund or charge full.",
  not_found: "Job not found — it may have been removed. Refresh the list.",
  no_payment: "This job has no funding payment on record, so there is nothing to release or charge.",
  unknown_action: "The gateway rejected the chosen action.",
};

export class CcResolveJob extends LitElement {
  static properties = {
    job: { attribute: false },
    _action: { state: true },
    _note: { state: true },
    _busy: { state: true },
    _result: { state: true },
    _error: { state: true },
  };

  constructor() {
    super();
    this.job = null;
    this._action = "refund_hold";
    this._note = "";
    this._busy = false;
    this._result = null;
    this._error = null;
    this._jobId = null;
  }

  createRenderRoot() {
    return this;
  }

  connectedCallback() {
    super.connectedCallback();
    this._onKey = (e) => {
      if (e.key === "Escape" && !this._busy) {
        e.stopPropagation();
        this._close();
      }
    };
    // Capture so the host's own Escape handler (e.g. the users drawer) does
    // not close the whole panel underneath us.
    window.addEventListener("keydown", this._onKey, true);
  }

  disconnectedCallback() {
    window.removeEventListener("keydown", this._onKey, true);
    super.disconnectedCallback();
  }

  willUpdate(changed) {
    // Reset only when a different job arrives. Hosts re-render (and may hand
    // us a fresh object) while refreshing after a success; the result view
    // must survive that.
    if (changed.has("job")) {
      const j = this._job();
      if (j.id === this._jobId) return;
      this._jobId = j.id;
      this._action = j.reported != null ? "accept_reported" : "refund_hold";
      this._note = "";
      this._busy = false;
      this._result = null;
      this._error = null;
    }
  }

  firstUpdated() {
    this.querySelector('input[type="radio"]:checked')?.focus();
  }

  /** Normalise attention (job_id) vs usage (id) rows into one shape. */
  _job() {
    const j = this.job || {};
    return {
      id: j.job_id || j.id || "",
      userEmail: j.user_email || null,
      userId: j.user_id || null,
      protocol: j.protocol || "",
      capability: j.capability || "",
      offering: j.offering || "",
      funded: toWei(j.funded_value_wei) ?? 0n,
      // Broker-reported units from the exchange lookup: present only on
      // open/unresolved rows, and exactly what accept_reported would bill.
      reported: j.reported_units == null ? null : Number(j.reported_units),
      unit: j.work_unit || "units",
      blocked: j.blocked_reason || null,
    };
  }

  _close() {
    this.dispatchEvent(new CustomEvent("cc-resolve-close", { bubbles: true, composed: false }));
  }

  async _submit(ev) {
    ev.preventDefault();
    if (this._busy || this._result) return;
    const j = this._job();
    this._busy = true;
    this._error = null;
    try {
      const result = await api.resolveJob(j.id, this._action, this._note);
      this._result = result;
      this.dispatchEvent(
        new CustomEvent("cc-job-resolved", {
          detail: { job_id: j.id, result },
          bubbles: true,
          composed: false,
        }),
      );
    } catch (err) {
      const reason = err?.details?.reason;
      this._error = {
        reason: reason || null,
        text: (reason && REASON_TEXT[reason]) || err?.message || "Resolve failed.",
        state: err?.details?.state || null,
      };
      if (reason === "no_broker_report" && this._action === "accept_reported") {
        this._action = "refund_hold";
      }
    } finally {
      this._busy = false;
    }
  }

  _renderFacts(j) {
    return html`
      <dl class="kv">
        <dt>Job</dt>
        <dd class="mono small" title=${j.id}>${j.id}</dd>
        <dt>User</dt>
        <dd title=${j.userId || ""}>${j.userEmail || html`<span class="mono small">${j.userId || "—"}</span>`}</dd>
        <dt>Offering</dt>
        <dd>
          <span class="cap-path">${j.capability}<span class="off">/${j.offering}</span></span>
          ${j.protocol ? html`<span class="muted small"> · ${j.protocol}</span>` : null}
        </dd>
        <dt>Funded</dt>
        <dd>${eth(j.funded, "warn-text")} <span class="muted small">ETH held</span></dd>
        <dt>Broker reported</dt>
        <dd>
          ${j.reported != null
            ? html`<span class="num">${formatCount(j.reported)}</span> <span class="muted small">${j.unit}</span>`
            : html`<span class="muted">no units on this row</span>`}
        </dd>
        ${j.blocked
          ? html`<dt>Blocked</dt>
              <dd><span class="tag" title=${j.blocked}>${blockedReasonLabel(j.blocked)}</span></dd>`
          : null}
      </dl>
    `;
  }

  _renderChoices(j) {
    return html`
      <fieldset class="choice-group" ?disabled=${this._busy}>
        <legend>Outcome</legend>
        ${ACTIONS.map(
          (a) => html`
            <label class="choice ${this._action === a.key ? "selected" : ""}">
              <input
                type="radio"
                name="resolve-action"
                value=${a.key}
                .checked=${this._action === a.key}
                @change=${() => (this._action = a.key)}
              />
              <span class="choice-body">
                <span class="choice-title">${a.label}</span>
                <span class="choice-explain">${a.explain(j)}</span>
              </span>
            </label>
          `,
        )}
      </fieldset>
    `;
  }

  _renderResult(r) {
    return html`
      <div class="msg ok">
        Resolved as <code>${r.outcome}</code>
        ${r.actual_units != null ? html` · ${formatCount(r.actual_units)} units` : null}
      </div>
      <dl class="kv mt-1">
        <dt>Billed</dt>
        <dd>${eth(r.billed_value_wei)} <span class="muted small">ETH</span></dd>
        <dt>Refunded</dt>
        <dd>${eth(r.refund_wei, (toWei(r.refund_wei) ?? 0n) > 0n ? "ok-text" : "muted")} <span class="muted small">ETH</span></dd>
        <dt>State</dt>
        <dd><span class="pill">${r.state}</span></dd>
      </dl>
    `;
  }

  render() {
    if (!this.job) return null;
    const j = this._job();
    const done = !!this._result;
    return html`
      <div class="modal-backdrop resolve-dialog" @click=${() => !this._busy && this._close()}>
        <form
          class="modal-card resolve-card"
          role="dialog"
          aria-modal="true"
          aria-labelledby="resolve-title"
          @click=${(e) => e.stopPropagation()}
          @submit=${this._submit}
        >
          <div class="card-head">
            <h3 id="resolve-title">Resolve stuck job</h3>
            <button type="button" class="close" aria-label="Close" ?disabled=${this._busy} @click=${this._close}>
              ${icon.x()}
            </button>
          </div>
          ${this._renderFacts(j)}
          ${done
            ? this._renderResult(this._result)
            : html`
                ${this._renderChoices(j)}
                <div class="field mt-2">
                  <label for="resolve-note">Note (optional)</label>
                  <textarea
                    id="resolve-note"
                    rows="2"
                    maxlength="500"
                    placeholder="Why you chose this outcome — lands in the audit log"
                    .value=${this._note}
                    ?disabled=${this._busy}
                    @input=${(e) => (this._note = e.target.value)}
                  ></textarea>
                </div>
                ${this._error
                  ? html`<div class="msg error mt-1" role="alert">${this._error.text}</div>`
                  : null}
              `}
          <div class="row resolve-actions mt-2">
            ${done
              ? html`<button type="button" class="primary" @click=${this._close}>Done</button>`
              : html`
                  <button type="button" class="ghost" ?disabled=${this._busy} @click=${this._close}>Cancel</button>
                  <button type="submit" class=${this._action === "refund_hold" ? "primary" : "warn"} ?disabled=${this._busy}>
                    ${this._busy ? "Resolving…" : "Confirm"}
                  </button>
                `}
          </div>
        </form>
      </div>
    `;
  }
}
customElements.define("cc-resolve-job", CcResolveJob);
