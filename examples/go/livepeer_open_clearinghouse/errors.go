package openclearinghouse

import "fmt"

// Error wraps Livepeer Open Clearinghouse's response envelope:
//
//	{"error": {"code": "...", "message": "...", "details": {...}}}
//
// Code is the canonical error code from the gateway (or empty if the
// response didn't carry one). Use errors.As/errors.Is or check the
// Code field directly. Convenience predicate methods cover the common
// cases.
type Error struct {
	// Code is the canonical error code from the gateway (e.g.
	// "INSUFFICIENT_CREDIT"), or empty when the response didn't carry one.
	Code string
	// Message is the human-readable message from the gateway, or a
	// synthesized "HTTP %d" when nothing better is available.
	Message string
	// Status is the HTTP status the gateway returned.
	Status int
	// Details is the structured details payload (may be empty).
	Details map[string]any
	// RetryAfterSeconds is the parsed Retry-After header, when present.
	RetryAfterSeconds int
}

// Error returns a one-line summary suitable for logging.
func (e *Error) Error() string {
	if e.Code != "" {
		return fmt.Sprintf("open-clearinghouse: %s (%s)", e.Message, e.Code)
	}
	return "open-clearinghouse: " + e.Message
}

// IsInsufficientCredit reports whether the gateway rejected the call
// because the user's balance was too low.
func (e *Error) IsInsufficientCredit() bool { return e.Code == "INSUFFICIENT_CREDIT" }

// IsSpendCapExceeded reports whether the per-period spend cap was hit.
func (e *Error) IsSpendCapExceeded() bool { return e.Code == "SPEND_CAP_EXCEEDED" }

// IsAccountNotApproved reports whether the user signed up but the
// operator hasn't approved them yet.
func (e *Error) IsAccountNotApproved() bool {
	return e.Code == "ACCOUNT_NOT_APPROVED" || e.Code == "account_not_approved"
}

// IsEmailNotVerified reports whether the user hasn't completed email
// verification.
func (e *Error) IsEmailNotVerified() bool { return e.Code == "email_not_verified" }

// IsNoRouteAvailable reports whether no orchestrator is advertising
// the requested capability+offering right now.
func (e *Error) IsNoRouteAvailable() bool { return e.Code == "NO_ROUTE_AVAILABLE" }

// IsRateLimited reports whether the gateway rate-limited the caller.
// When true, check RetryAfterSeconds.
func (e *Error) IsRateLimited() bool { return e.Code == "rate_limited" || e.Status == 429 }

// IsDuplicateRequest reports whether the same Idempotency-Key was seen
// with different inputs.
func (e *Error) IsDuplicateRequest() bool { return e.Code == "DUPLICATE_REQUEST" }

// IsDaemonUnavailable reports whether Livepeer Open Clearinghouse couldn't reach
// payment-daemon or registry-daemon.
func (e *Error) IsDaemonUnavailable() bool {
	return e.Code == "DAEMON_UNAVAILABLE" || e.Status == 502 || e.Status == 503
}
