// Shared formatting helpers for the portal SPA.
//
// All wei amounts arrive from the API as integer strings (never numbers,
// never exponent notation). Everything here parses with BigInt so no
// precision is lost; only the *display* string is lossy.

const WEI_PER_ETH = 10n ** 18n;
const ETH_DECIMALS = 18;
// How many significant fractional digits to show. Job costs are tiny
// (hundreds of gwei), so this counts from the first non-zero fractional
// digit rather than from the decimal point — otherwise nearly everything
// would render as "<0.000001 ETH".
const SIG_DECIMALS = 6;
// Amounts under 1 gwei are shown as "N wei" rather than 0.000000000000000035 ETH.
const WEI_TIER_FLOOR = 10n ** 9n;

/** Parse an integer-string wei value into a BigInt. Returns null on junk. */
export function toWei(value) {
  if (value == null || value === "") return null;
  try {
    return BigInt(String(value));
  } catch {
    return null;
  }
}

/** Group thousands: "830000000000" → "830,000,000,000". */
export function groupDigits(digits) {
  return String(digits).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/** Exact wei with thousands separators, for tooltips: "830,000,000,000 wei". */
export function formatWeiExact(value) {
  const wei = toWei(value);
  if (wei == null) return "—";
  const neg = wei < 0n;
  const abs = neg ? -wei : wei;
  return `${neg ? "-" : ""}${groupDigits(abs.toString())} wei`;
}

/**
 * Render a wei string as ETH with up to SIG_DECIMALS significant fractional
 * digits, trailing zeros trimmed. Truncates rather than rounds so a user is
 * never shown more than they actually have; the exact wei is available via
 * formatWeiExact() for a `title` tooltip.
 *
 *   "830000000000"      → "0.00000083 ETH"
 *   "24000000000000"    → "0.000024 ETH"
 *   "1500000000000000000" → "1.5 ETH"
 *   "0"                 → "0 ETH"
 *   "35"                → "35 wei"   (sub-gwei amounts stay in wei)
 */
export function formatEth(value, { unit = true } = {}) {
  const wei = toWei(value);
  if (wei == null) return "—";
  const neg = wei < 0n;
  const abs = neg ? -wei : wei;
  // Below one gwei an ETH rendering is a wall of zeros; say it in wei instead.
  if (abs > 0n && abs < WEI_TIER_FLOOR) {
    return `${neg ? "-" : ""}${groupDigits(abs.toString())}${unit ? " wei" : ""}`;
  }
  const whole = abs / WEI_PER_ETH;
  let frac = (abs % WEI_PER_ETH).toString().padStart(ETH_DECIMALS, "0");

  let keep;
  if (whole > 0n) {
    keep = SIG_DECIMALS;
  } else {
    const firstNonZero = frac.search(/[1-9]/);
    keep = firstNonZero < 0 ? 0 : Math.min(ETH_DECIMALS, firstNonZero + SIG_DECIMALS);
  }
  frac = frac.slice(0, keep).replace(/0+$/, "");

  const body = frac ? `${groupDigits(whole.toString())}.${frac}` : groupDigits(whole.toString());
  return `${neg ? "-" : ""}${body}${unit ? " ETH" : ""}`;
}

/**
 * Amount for a table cell whose header already says "(ETH)": the ETH figure
 * without its unit, except sub-gwei amounts which keep their "wei" unit so
 * they are never misread as ETH.
 */
export function formatEthCell(value) {
  const wei = toWei(value);
  if (wei == null) return "—";
  const abs = wei < 0n ? -wei : wei;
  return formatEth(value, { unit: !(abs === 0n || abs >= WEI_TIER_FLOOR) });
}

/** Wei → Number of ETH, for plotting only (lossy by design). */
export function weiToEthNumber(value) {
  const wei = toWei(value);
  if (wei == null) return 0;
  // Split so very large balances don't lose the fractional part.
  const whole = Number(wei / WEI_PER_ETH);
  const frac = Number(wei % WEI_PER_ETH) / 1e18;
  return whole + frac;
}

/** Sum an iterable of wei strings as a BigInt. */
export function sumWei(values) {
  let total = 0n;
  for (const v of values) {
    const w = toWei(v);
    if (w != null) total += w;
  }
  return total;
}

/**
 * Percentage of `part` over `whole`, both wei strings, to two decimals.
 * Returns null when whole is missing or zero.
 */
export function percentOf(part, whole) {
  const p = toWei(part);
  const w = toWei(whole);
  if (p == null || w == null || w === 0n) return null;
  return Number((p * 10000n) / w) / 100;
}

// ----- ledger reasons → user language -----

const LEDGER_REASONS = {
  topup: "Credit added",
  auto_replenish: "Auto top-up",
  initial_credit: "Welcome credit",
  payment_charge: "Job funded",
  payment_refund: "Unused funds returned",
  session_encumbrance: "Session funds held",
  session_release: "Session funds released",
};

/** Human label for a ledger reason code; unknown codes fall back to the raw code. */
export function ledgerReasonLabel(reason) {
  return LEDGER_REASONS[reason] || reason || "—";
}

/** Pill tone for a ledger reason (matches .pill.* variants in portal.css). */
export function ledgerReasonTone(reason) {
  switch (reason) {
    case "topup":
    case "auto_replenish":
    case "initial_credit":
      return "ok";
    case "payment_charge":
      return "info";
    case "payment_refund":
    case "session_release":
      return "warn";
    case "session_encumbrance":
      return "role";
    default:
      return "";
  }
}

// ----- job accounting outcome → pill -----

/**
 * { label, tone } for a usage job. A job that is still open or draining is
 * "open" whatever the backend's provisional accounting_outcome says (the live
 * API reports "unresolved" for open jobs); the raw outcome belongs in a tooltip.
 */
export function jobOutcomePill(job) {
  if (job && job.state && job.state !== "closed") return { label: "open", tone: "info" };
  switch (job?.accounting_outcome) {
    case "broker_settled":
      return { label: "settled", tone: "ok" };
    case "conservative_full_charge":
      return { label: "conservative charge", tone: "bad" };
    case "unresolved":
      return { label: "unresolved", tone: "warn" };
    case "open":
    default:
      return { label: "open", tone: "info" };
  }
}

// ----- dates -----

export function formatDateTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleString();
}

/** Compact date+time for dense tables: "9/8/26, 8:20 AM". */
export function formatDateTimeShort(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? String(iso)
    : d.toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" });
}

export function formatDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleDateString();
}

/** "YYYY-MM-DD" → short label like "Aug 12" (locale-aware). */
export function formatDay(day) {
  if (!day) return "—";
  const d = new Date(`${day}T00:00:00`);
  return Number.isNaN(d.getTime())
    ? String(day)
    : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function formatDuration(seconds) {
  if (seconds == null || Number.isNaN(Number(seconds))) return "—";
  const s = Math.round(Number(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

/** Integer with thousands separators; null → "—". */
export function formatInt(n) {
  if (n == null || n === "") return "—";
  return groupDigits(String(Math.trunc(Number(n))));
}

// ----- CSV -----

/** Quote a CSV cell (RFC 4180). */
export function csvCell(value) {
  if (value == null) return "";
  const s = String(value);
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

export function toCsv(header, rows) {
  const lines = [header.map(csvCell).join(",")];
  for (const row of rows) lines.push(row.map(csvCell).join(","));
  return lines.join("\r\n") + "\r\n";
}

/** Offer `text` as a file download via a transient Blob link. */
export function downloadText(filename, text, mime = "text/csv;charset=utf-8") {
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
