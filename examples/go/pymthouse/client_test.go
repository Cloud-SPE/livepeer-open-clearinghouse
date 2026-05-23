package pymthouse_test

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/livepeer/pymthouse-sdk-go/pymthouse"
)

const apiKey = "pymth_live_test_key"

func newServerClient(t *testing.T, h http.Handler) *pymthouse.Client {
	t.Helper()
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	c, err := pymthouse.NewClient(pymthouse.Options{
		BaseURL: srv.URL,
		APIKey:  apiKey,
	})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	return c
}

func TestMintPaymentHappyPath(t *testing.T) {
	c := newServerClient(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/payments/mint" || r.Method != http.MethodPost {
			t.Fatalf("unexpected: %s %s", r.Method, r.URL.Path)
		}
		if got := r.Header.Get("X-API-Key"); got != apiKey {
			t.Fatalf("bad api key: %q", got)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		w.Write([]byte(`{
			"payment_id": "00000000-0000-0000-0000-000000000001",
			"work_id": "deadbeef",
			"payment_bytes": "AAAA",
			"expected_value_wei": "244140",
			"funded_value_wei": "25000000000",
			"recipient_eth_address": "0xd003"
		}`))
	}))
	mint, err := c.MintPayment(context.Background(), pymthouse.MintPaymentInput{
		Capability: "openai:chat-completions",
		Offering:   "vllm-qwen3.6-27b-default",
		WorkUnits:  1000,
	})
	if err != nil {
		t.Fatalf("MintPayment: %v", err)
	}
	if mint.PaymentBytes != "AAAA" {
		t.Errorf("payment_bytes: got %q", mint.PaymentBytes)
	}
	if mint.RecipientEthAddress != "0xd003" {
		t.Errorf("recipient: got %q", mint.RecipientEthAddress)
	}
}

func TestInsufficientCredit(t *testing.T) {
	c := newServerClient(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusPaymentRequired)
		w.Write([]byte(`{"error":{"code":"INSUFFICIENT_CREDIT","message":"0 < 1000","details":{"available_wei":"0","required_wei":"1000"}}}`))
	}))
	_, err := c.MintPayment(context.Background(), pymthouse.MintPaymentInput{
		Capability: "x", Offering: "y", WorkUnits: 1,
	})
	phErr, ok := err.(*pymthouse.Error)
	if !ok {
		t.Fatalf("expected *Error, got %T (%v)", err, err)
	}
	if !phErr.IsInsufficientCredit() {
		t.Errorf("expected IsInsufficientCredit; got code=%q", phErr.Code)
	}
	if phErr.Status != 402 {
		t.Errorf("expected status 402; got %d", phErr.Status)
	}
	if phErr.Details["required_wei"] != "1000" {
		t.Errorf("details lost: %#v", phErr.Details)
	}
}

func TestRateLimitedCarriesRetryAfter(t *testing.T) {
	c := newServerClient(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Retry-After", "12")
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusTooManyRequests)
		w.Write([]byte(`{"detail":"rate_limited"}`))
	}))
	_, err := c.MintPayment(context.Background(), pymthouse.MintPaymentInput{
		Capability: "x", Offering: "y", WorkUnits: 1,
	})
	phErr, ok := err.(*pymthouse.Error)
	if !ok {
		t.Fatalf("expected *Error, got %T (%v)", err, err)
	}
	if !phErr.IsRateLimited() {
		t.Errorf("expected IsRateLimited")
	}
	if phErr.RetryAfterSeconds != 12 {
		t.Errorf("retry after: got %d", phErr.RetryAfterSeconds)
	}
}

func TestIdempotencyKeyThreaded(t *testing.T) {
	var seen string
	c := newServerClient(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = r.Header.Get("Idempotency-Key")
		w.WriteHeader(http.StatusCreated)
		w.Write([]byte(`{"payment_id":"00000000-0000-0000-0000-000000000001","work_id":"x","payment_bytes":"AAAA","expected_value_wei":"1","funded_value_wei":"1","recipient_eth_address":"0xd003"}`))
	}))
	if _, err := c.MintPayment(context.Background(), pymthouse.MintPaymentInput{
		Capability: "x", Offering: "y", WorkUnits: 1,
		IdempotencyKey: "abc-123",
	}); err != nil {
		t.Fatalf("MintPayment: %v", err)
	}
	if seen != "abc-123" {
		t.Errorf("idempotency-key not threaded; got %q", seen)
	}
}

func TestNewClientRejectsBadKey(t *testing.T) {
	_, err := pymthouse.NewClient(pymthouse.Options{
		BaseURL: "http://x", APIKey: "not-a-real-key",
	})
	if err == nil || !strings.Contains(err.Error(), "pymth_") {
		t.Errorf("expected pymth_ rejection; got %v", err)
	}
}

func TestListCapabilitiesUnwrapsItems(t *testing.T) {
	c := newServerClient(t, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"items":[{"name":"openai:chat-completions","work_unit":"tokens","offerings":[]}]}`))
	}))
	caps, err := c.ListCapabilities(context.Background())
	if err != nil {
		t.Fatalf("list capabilities: %v", err)
	}
	if len(caps) != 1 || caps[0].Name != "openai:chat-completions" {
		t.Errorf("unexpected: %+v", caps)
	}
}

func TestListOrchestratorsPassesCapabilityFilter(t *testing.T) {
	var seenQuery string
	c := newServerClient(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seenQuery = r.URL.RawQuery
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"items":[]}`))
	}))
	if _, err := c.ListOrchestrators(context.Background(), "openai:chat-completions"); err != nil {
		t.Fatalf("list orchestrators: %v", err)
	}
	if !strings.Contains(seenQuery, "capability=openai%3Achat-completions") {
		t.Errorf("query missing capability filter: %q", seenQuery)
	}
}

func TestReportUsageReturnsRefund(t *testing.T) {
	c := newServerClient(t, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"refunded_wei":"12345","payment_status":"settled","new_balance_wei":"999999","usage":{"id":"u1","actual_work_units":800,"final_charge_wei":"20000"}}`))
	}))
	res, err := c.ReportUsage(context.Background(), pymthouse.ReportUsageInput{
		PaymentID:       "00000000-0000-0000-0000-000000000001",
		ActualWorkUnits: 800,
		IdempotencyKey:  "abc-123",
	})
	if err != nil {
		t.Fatalf("report usage: %v", err)
	}
	if res.RefundedWei != "12345" || res.NewBalanceWei != "999999" {
		t.Errorf("unexpected: %+v", res)
	}
}

func TestNonJSONErrorBodyFallsBackToText(t *testing.T) {
	c := newServerClient(t, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte("upstream down"))
	}))
	_, err := c.MintPayment(context.Background(), pymthouse.MintPaymentInput{
		Capability: "x", Offering: "y", WorkUnits: 1,
	})
	phErr, ok := err.(*pymthouse.Error)
	if !ok {
		t.Fatalf("expected *Error, got %T", err)
	}
	if phErr.Status != 503 {
		t.Errorf("expected 503; got %d", phErr.Status)
	}
}
