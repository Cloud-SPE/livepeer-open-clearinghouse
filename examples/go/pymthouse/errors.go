package pymthouse

import "fmt"

// Error wraps PymtHouse's response envelope:
//
//	{"error": {"code": "...", "message": "...", "details": {...}}}
//
// Code is the canonical error code from the gateway (or nil if the
// response didn't carry one). Use errors.As / errors.Is or check the
// Code field directly. Convenience predicate methods cover the common
// cases.
type Error struct {
	Code             string
	Message          string
	Status           int
	Details          map[string]any
	RetryAfterSeconds int
}

func (e *Error) Error() string {
	if e.Code != "" {
		return fmt.Sprintf("pymthouse: %s (%s)", e.Message, e.Code)
	}
	return "pymthouse: " + e.Message
}

func (e *Error) IsInsufficientCredit() bool { return e.Code == "INSUFFICIENT_CREDIT" }
func (e *Error) IsSpendCapExceeded() bool   { return e.Code == "SPEND_CAP_EXCEEDED" }
func (e *Error) IsAccountNotApproved() bool {
	return e.Code == "ACCOUNT_NOT_APPROVED" || e.Code == "account_not_approved"
}
func (e *Error) IsEmailNotVerified() bool { return e.Code == "email_not_verified" }
func (e *Error) IsNoRouteAvailable() bool { return e.Code == "NO_ROUTE_AVAILABLE" }
func (e *Error) IsRateLimited() bool      { return e.Code == "rate_limited" || e.Status == 429 }
func (e *Error) IsDuplicateRequest() bool { return e.Code == "DUPLICATE_REQUEST" }
func (e *Error) IsDaemonUnavailable() bool {
	return e.Code == "DAEMON_UNAVAILABLE" || e.Status == 502 || e.Status == 503
}
