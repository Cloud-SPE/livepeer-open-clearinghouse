package openclearinghouse_test

import (
	"strings"
	"testing"

	openclearinghouse "github.com/livepeer/livepeer-open-clearinghouse-sdk-go/livepeer_open_clearinghouse"
)

// TestErrorPredicates walks every IsX() method so the predicate logic is
// covered without waiting for the corresponding error to occur in
// real traffic.
func TestErrorPredicates(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name      string
		code      string
		status    int
		predicate func(*openclearinghouse.Error) bool
	}{
		{"InsufficientCredit", "INSUFFICIENT_CREDIT", 402, (*openclearinghouse.Error).IsInsufficientCredit},
		{"SpendCapExceeded", "SPEND_CAP_EXCEEDED", 402, (*openclearinghouse.Error).IsSpendCapExceeded},
		{"AccountNotApproved", "ACCOUNT_NOT_APPROVED", 403, (*openclearinghouse.Error).IsAccountNotApproved},
		{"AccountNotApprovedLower", "account_not_approved", 403, (*openclearinghouse.Error).IsAccountNotApproved},
		{"EmailNotVerified", "email_not_verified", 403, (*openclearinghouse.Error).IsEmailNotVerified},
		{"NoRouteAvailable", "NO_ROUTE_AVAILABLE", 404, (*openclearinghouse.Error).IsNoRouteAvailable},
		{"RateLimitedCode", "rate_limited", 429, (*openclearinghouse.Error).IsRateLimited},
		{"RateLimitedStatusOnly", "", 429, (*openclearinghouse.Error).IsRateLimited},
		{"DuplicateRequest", "DUPLICATE_REQUEST", 409, (*openclearinghouse.Error).IsDuplicateRequest},
		{"DaemonUnavailable", "DAEMON_UNAVAILABLE", 502, (*openclearinghouse.Error).IsDaemonUnavailable},
		{"DaemonUnavailable503", "", 503, (*openclearinghouse.Error).IsDaemonUnavailable},
	}
	for _, c := range cases {
		c := c
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			err := &openclearinghouse.Error{Code: c.code, Status: c.status, Message: "x"}
			if !c.predicate(err) {
				t.Errorf("expected %s predicate true; got false (code=%q status=%d)", c.name, c.code, c.status)
			}
		})
	}
}

func TestErrorErrorFormat(t *testing.T) {
	t.Parallel()
	t.Run("with code", func(t *testing.T) {
		t.Parallel()
		err := &openclearinghouse.Error{Code: "X", Message: "bad", Status: 500}
		if got := err.Error(); !strings.Contains(got, "X") || !strings.Contains(got, "bad") {
			t.Errorf("unexpected: %q", got)
		}
	})
	t.Run("without code", func(t *testing.T) {
		t.Parallel()
		err := &openclearinghouse.Error{Message: "boom", Status: 500}
		if got := err.Error(); got != "open-clearinghouse: boom" {
			t.Errorf("unexpected: %q", got)
		}
	})
}
