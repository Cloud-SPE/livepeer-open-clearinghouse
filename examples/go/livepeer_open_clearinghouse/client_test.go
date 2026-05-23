package openclearinghouse_test

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	openclearinghouse "github.com/livepeer/livepeer-open-clearinghouse-sdk-go/livepeer_open_clearinghouse"
)

const apiKey = "pymth_live_test_key"

func newServerClient(t *testing.T, h http.Handler) *openclearinghouse.Client {
	t.Helper()
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	c, err := openclearinghouse.NewClient(openclearinghouse.Options{
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
	mint, err := c.MintPayment(context.Background(), openclearinghouse.MintPaymentInput{
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
	_, err := c.MintPayment(context.Background(), openclearinghouse.MintPaymentInput{
		Capability: "x", Offering: "y", WorkUnits: 1,
	})
	phErr, ok := err.(*openclearinghouse.Error)
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
	_, err := c.MintPayment(context.Background(), openclearinghouse.MintPaymentInput{
		Capability: "x", Offering: "y", WorkUnits: 1,
	})
	phErr, ok := err.(*openclearinghouse.Error)
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
	if _, err := c.MintPayment(context.Background(), openclearinghouse.MintPaymentInput{
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
	_, err := openclearinghouse.NewClient(openclearinghouse.Options{
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
	res, err := c.ReportUsage(context.Background(), openclearinghouse.ReportUsageInput{
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
	_, err := c.MintPayment(context.Background(), openclearinghouse.MintPaymentInput{
		Capability: "x", Offering: "y", WorkUnits: 1,
	})
	phErr, ok := err.(*openclearinghouse.Error)
	if !ok {
		t.Fatalf("expected *Error, got %T", err)
	}
	if phErr.Status != 503 {
		t.Errorf("expected 503; got %d", phErr.Status)
	}
}

// TestSubmitJobRetriesOnInvalidRecipientRand drives one route, two mints, and
// two orch POSTs through a single test server. The first orch POST returns
// 401 with INVALID_RECIPIENT_RAND in the body; the SDK should re-mint with
// a fresh idempotency key, retry once, and surface the resulting 200.
func TestSubmitJobRetriesOnInvalidRecipientRand(t *testing.T) {
	var mintCount, orchCount int
	var seenPayments []string

	// Orch server (separate from the gateway server).
	orch := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		orchCount++
		seenPayments = append(seenPayments, r.Header.Get("Livepeer-Payment"))
		if orchCount == 1 {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusUnauthorized)
			_, _ = w.Write([]byte(`{"error":{"code":"payment_invalid","message":"INVALID_RECIPIENT_RAND: rotated"}}`))
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"model":"Qwen","choices":[]}`))
	}))
	t.Cleanup(orch.Close)

	c := newServerClient(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/routes":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"eth_address":"0xd003","worker_url":"` + orch.URL + `","capability":"x","offering":"y","price_per_work_unit_wei":"1"}`))
		case "/v1/payments/mint":
			mintCount++
			bytesField := "FIRST"
			if mintCount > 1 {
				bytesField = "SECOND"
			}
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusCreated)
			fmt.Fprintf(w, `{"payment_id":"00000000-0000-0000-0000-00000000000%d","work_id":"abc","payment_bytes":"%s","expected_value_wei":"1","funded_value_wei":"1","recipient_eth_address":"0xd003"}`, mintCount, bytesField)
		default:
			t.Fatalf("unexpected gateway path: %s", r.URL.Path)
		}
	}))

	result, err := c.SubmitJob(context.Background(), openclearinghouse.SubmitJobInput{
		Capability:  "x",
		Offering:    "y",
		WorkUnits:   1,
		Body:        []byte(`{"messages":[]}`),
		ContentType: "application/json",
	})
	if err != nil {
		t.Fatalf("SubmitJob: %v", err)
	}
	if mintCount != 2 {
		t.Errorf("expected 2 mints; got %d", mintCount)
	}
	if orchCount != 2 {
		t.Errorf("expected 2 orch posts; got %d", orchCount)
	}
	if len(seenPayments) != 2 || seenPayments[0] == seenPayments[1] {
		t.Errorf("expected distinct payment_bytes per attempt; got %v", seenPayments)
	}
	if result.Status != 200 {
		t.Errorf("expected final status 200; got %d", result.Status)
	}
}

// TestSubmitJobDoesNotRetryOnUnrelated401 confirms the retry trigger is
// narrow — a 401 without INVALID_RECIPIENT_RAND must surface as-is.
func TestSubmitJobDoesNotRetryOnUnrelated401(t *testing.T) {
	var mintCount int
	orch := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"error":{"code":"bad_token","message":"expired"}}`))
	}))
	t.Cleanup(orch.Close)
	c := newServerClient(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/routes":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"eth_address":"0xd003","worker_url":"` + orch.URL + `","capability":"x","offering":"y","price_per_work_unit_wei":"1"}`))
		case "/v1/payments/mint":
			mintCount++
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"payment_id":"00000000-0000-0000-0000-000000000001","work_id":"abc","payment_bytes":"AAAA","expected_value_wei":"1","funded_value_wei":"1","recipient_eth_address":"0xd003"}`))
		}
	}))
	result, err := c.SubmitJob(context.Background(), openclearinghouse.SubmitJobInput{
		Capability: "x", Offering: "y", WorkUnits: 1,
		Body: []byte(`{"messages":[]}`), ContentType: "application/json",
	})
	if err != nil {
		t.Fatalf("SubmitJob: %v", err)
	}
	if mintCount != 1 {
		t.Errorf("expected 1 mint; got %d", mintCount)
	}
	if result.Status != 401 {
		t.Errorf("expected status 401; got %d", result.Status)
	}
}
