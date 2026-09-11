// Shared formatting helpers for the admin SPA.
//
// All wei amounts arrive from the API as integer strings. Never coerce them
// to Number — a 1 ETH balance (1e18) already exceeds Number.MAX_SAFE_INTEGER.
// Everything here goes through BigInt.

import { html } from "lit";

const E18 = 1_000_000_000_000_000_000n;

function group(digits) {
  return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/** Parse a wei value (string | bigint | number) into a BigInt, or null. */
export function toWei(value) {
  if (value == null || value === "") return null;
  if (typeof value === "bigint") return value;
  try {
    return BigInt(String(value).split(".")[0]);
  } catch {
    return null;
  }
}

/** Sum an array of wei values (strings) into a BigInt. */
export function sumWei(values) {
  let total = 0n;
  for (const v of values) total += toWei(v) ?? 0n;
  return total;
}

/** "1,234,567 wei" — exact integer with thousands separators. */
export function formatWei(value) {
  const n = toWei(value);
  if (n == null) return "—";
  const sign = n < 0n ? "-" : "";
  const abs = n < 0n ? -n : n;
  return `${sign}${group(abs.toString())}`;
}

/**
 * ETH with up to 6 significant decimals.
 *
 * - 1.5 ETH            -> "1.5"
 * - 1234567890000000000 -> "1.234568" (6 decimal places once ≥ 1e-6 ETH)
 * - 1234 wei           -> "0.000000000000001234" (6 significant digits kept
 *                          after the leading zeros so tiny charges still read)
 */
export function formatEth(value) {
  const n = toWei(value);
  if (n == null) return "—";
  const sign = n < 0n ? "-" : "";
  const abs = n < 0n ? -n : n;
  const whole = group((abs / E18).toString());
  const frac = (abs % E18).toString().padStart(18, "0");
  let keep;
  if (abs / E18 > 0n) {
    keep = 6;
  } else {
    const firstNonZero = frac.search(/[1-9]/);
    keep = firstNonZero < 0 ? 0 : Math.min(18, firstNonZero + 6);
  }
  // Truncate (not round) so what we show never exceeds the exact value.
  const fracStr = frac.slice(0, keep).replace(/0+$/, "");
  return fracStr ? `${sign}${whole}.${fracStr}` : `${sign}${whole}`;
}

/** ETH figure as a tabular-numeral span whose tooltip carries the exact wei. */
export function eth(value, extraClass = "") {
  const n = toWei(value);
  if (n == null) return html`<span class="num muted">—</span>`;
  return html`<span class="num ${extraClass}" title="${formatWei(n)} wei">${formatEth(n)}</span>`;
}

/** Integer count with thousands separators (numbers or numeric strings). */
export function formatCount(value) {
  if (value == null || value === "") return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  return n.toLocaleString();
}

/** Relative time: "12s ago", "3m ago", "5h ago", "2d ago". */
export function timeAgo(iso) {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(ms)) return "—";
  return formatDuration(Math.max(0, Math.round(ms / 1000))) + " ago";
}

/** Seconds -> "45s", "3m", "5h", "2d 3h". */
export function formatDuration(seconds) {
  if (seconds == null || !Number.isFinite(Number(seconds))) return "—";
  const s = Math.round(Number(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m`;
  const d = Math.floor(h / 24);
  return `${d}d ${h % 24}h`;
}

/** Local date+time, short. */
export function formatDateTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Local date only. */
export function formatDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** "YYYY-MM-DD" day key -> "Sep 8". Avoids TZ shifting by parsing as local. */
export function formatDay(day) {
  if (!day) return "—";
  const [y, m, d] = String(day).split("-").map(Number);
  if (!y || !m || !d) return String(day);
  return new Date(y, m - 1, d).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
}

/** Percentage from a 0..1 or 0..100 number; null-safe. */
export function formatPct(value) {
  if (value == null || !Number.isFinite(Number(value))) return "—";
  const n = Number(value);
  const pct = n <= 1 ? n * 100 : n;
  return `${pct.toFixed(pct < 10 ? 1 : 0)}%`;
}

/**
 * Map a job to its display state and pill class.
 * Priority: accounting outcome for closed jobs, lifecycle state otherwise.
 */
export function jobStatus(job) {
  const outcome = job?.accounting_outcome;
  const state = job?.state;
  if (outcome === "unresolved") return { label: "unresolved", pill: "bad" };
  if (outcome === "conservative_full_charge") return { label: "conservative charge", pill: "warn" };
  if (outcome === "broker_settled") return { label: "settled", pill: "ok" };
  if (state === "draining") return { label: "draining", pill: "info" };
  if (state === "open" || outcome === "open") return { label: "open", pill: "info" };
  if (state === "closed") return { label: "closed", pill: "" };
  return { label: state || outcome || "unknown", pill: "" };
}

/**
 * Whether an operator may resolve this row: the reconciler gave up on it
 * (blocked_reason set) or it has been open past the stale window
 * (accounting_outcome "unresolved"). Closed rows are never resolvable.
 */
export function isResolvable(job) {
  if (!job) return false;
  if (job.state === "closed") return false;
  return job.blocked_reason != null || job.accounting_outcome === "unresolved";
}

/** "missing_delegation" -> "missing delegation" for the muted tag. */
export function blockedReasonLabel(reason) {
  if (!reason) return "";
  return String(reason).replace(/_/g, " ");
}
