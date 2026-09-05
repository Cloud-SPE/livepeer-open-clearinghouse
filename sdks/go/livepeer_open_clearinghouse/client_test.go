// Tests for the handoff-mode Go SDK. Uses net/http/httptest for both
// the LOC gateway and the broker.
package openclearinghouse_test

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	loc "github.com/livepeer/livepeer-open-clearinghouse-sdk-go/livepeer_open_clearinghouse"
)

const apiKey = "pymth_live_test"
const encodedTestSettlement = "eyJwYXlsb2FkIjp7fSwic2lnbmF0dXJlIjp7fX0="

func locOpenJob(t *testing.T, brokerURL, transport string) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/jobs", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Fatalf("unexpected method %s on /v1/jobs", r.Method)
		}
		if got := r.Header.Get("Livepeer-Open-Clearinghouse-SDK"); !strings.HasPrefix(got, "go/") {
			t.Errorf("expected SDK identity to start with go/, got %q", got)
		}
		if r.Header.Get("Idempotency-Key") == "" {
			t.Error("missing LOC Idempotency-Key")
		}
		var openBody map[string]any
		_ = json.NewDecoder(r.Body).Decode(&openBody)
		if openBody["transport"] != transport {
			t.Errorf("open transport = %v; want %s", openBody["transport"], transport)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(201)
		_ = json.NewEncoder(w).Encode(map[string]any{
			"job_id":             "00000000-0000-0000-0000-000000000abc",
			"request_id":         "broker-request-1",
			"work_id":            "wid-abc",
			"broker_url":         brokerURL,
			"protocol":           "paid-job/v1",
			"transport":          transport,
			"work_unit":          "token",
			"payment_envelope":   "BASE64ENV",
			"expected_value_wei": 100000,
			"funded_value_wei":   100000,
			"settle_endpoint":    "/v1/jobs/00000000-0000-0000-0000-000000000abc/settle",
			"opened_at":          "2026-05-24T12:00:00Z",
		})
	})
	mux.HandleFunc("/v1/jobs/00000000-0000-0000-0000-000000000abc/settle", func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		if body["broker_job_id"] != "broker-job-1" || body["work_unit"] != "token" {
			t.Errorf("settle audit fields: %v", body)
		}
		if _, ok := body["settlement"].(map[string]any); !ok {
			t.Errorf("settle missing decoded signed settlement: %v", body)
		}
		au, _ := body["actual_units"].(float64)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"job_id":           "00000000-0000-0000-0000-000000000abc",
			"work_id":          "wid-abc",
			"actual_units":     au,
			"billed_value_wei": au * 1000,
			"refund_wei":       100000 - au*1000,
			"outcome":          "OVERFUNDED",
			"closed_at":        "2026-05-24T12:00:30Z",
			"cap_status": map[string]any{
				"session_pct_used":        au / 100,
				"spend_period_pct_used":   nil,
				"user_balance_pct_used":   nil,
				"operator_pool_pct_used":  nil,
				"will_refuse_next_refill": false,
				"winddown_reason":         nil,
			},
		})
	})
	return httptest.NewServer(mux)
}

func brokerServer(t *testing.T, status int) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/job" {
			t.Fatalf("unexpected broker path %s", r.URL.Path)
		}
		if got := r.Header.Get("Livepeer-Payment"); got != "BASE64ENV" {
			t.Errorf("expected Livepeer-Payment BASE64ENV, got %q", got)
		}
		if r.Header.Get("Livepeer-Protocol") != "paid-job/v1" || r.Header.Get("Livepeer-Mode") != "" || r.Header.Get("Livepeer-Spec-Version") != "" {
			t.Errorf("unexpected protocol headers: %v", r.Header)
		}
		if r.Header.Get("Livepeer-Request-Id") != "broker-request-1" {
			t.Errorf("broker request id = %q", r.Header.Get("Livepeer-Request-Id"))
		}
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Livepeer-Work-Units", "42")
		w.Header().Set("Livepeer-Work-Unit", "token")
		w.Header().Set("Livepeer-Job-Id", "broker-job-1")
		w.Header().Set("Livepeer-Settlement", encodedTestSettlement)
		w.WriteHeader(status)
		_, _ = w.Write([]byte(`{"reply":"ok"}`))
	}))
}

func TestNewClientRejectsBadKey(t *testing.T) {
	if _, err := loc.NewClient(loc.Options{BaseURL: "x", APIKey: "nope"}); err == nil {
		t.Fatal("expected error for malformed key")
	}
}

func TestSubmitJobHappyPath(t *testing.T) {
	broker := brokerServer(t, 200)
	defer broker.Close()
	loca := locOpenJob(t, broker.URL, "unary")
	defer loca.Close()

	client, err := loc.NewClient(loc.Options{BaseURL: loca.URL, APIKey: apiKey})
	if err != nil {
		t.Fatal(err)
	}
	result, err := client.SubmitJob(context.Background(), loc.SubmitJobInput{
		Capability:     "openai:chat-completions",
		Offering:       "gpt-oss-20b",
		EstimatedUnits: 80,
		MaxTotalUnits:  100,
		Body:           []byte(`{"prompt":"hello"}`),
	})
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != 200 {
		t.Errorf("status: got %d, want 200", result.Status)
	}
	if result.ActualUnits != 42 {
		t.Errorf("actual_units: got %d, want 42", result.ActualUnits)
	}
	if result.BilledValueWei != 42000 {
		t.Errorf("billed: got %d, want 42000", result.BilledValueWei)
	}
	if result.RefundWei != 58000 {
		t.Errorf("refund: got %d, want 58000", result.RefundWei)
	}
	if result.Outcome != "OVERFUNDED" {
		t.Errorf("outcome: got %q, want OVERFUNDED", result.Outcome)
	}
	if result.Protocol != "paid-job/v1" || result.Transport != "unary" || result.WorkUnit != "token" {
		t.Fatalf("v1 audit fields: protocol=%q transport=%q unit=%q", result.Protocol, result.Transport, result.WorkUnit)
	}
	if result.BrokerJobID != "broker-job-1" || result.RequestID != "broker-request-1" {
		t.Fatalf("broker ids: job=%q request=%q", result.BrokerJobID, result.RequestID)
	}
	if result.CapStatus.SessionPctUsed < 0.4 || result.CapStatus.SessionPctUsed > 0.5 {
		t.Errorf("session_pct_used out of range: %v", result.CapStatus.SessionPctUsed)
	}
}

func TestSubmitJobStreamReadsTerminalTrailers(t *testing.T) {
	settlement := map[string]any{
		"payload":   map[string]any{"work_id": "wid-abc", "debited_units": "7"},
		"signature": map[string]any{"algorithm": "secp256k1", "canonicalization": "jcs", "value": "0xsigned"},
	}
	rawSettlement, _ := json.Marshal(settlement)
	broker := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Accept") != "text/event-stream" {
			t.Errorf("Accept = %q", r.Header.Get("Accept"))
		}
		w.Header().Set("Content-Type", "text/event-stream")
		w.Header().Set("Livepeer-Job-Id", "broker-job-1")
		w.Header().Set("Livepeer-Work-Unit", "token")
		w.Header().Add("Trailer", "Livepeer-Work-Units")
		w.Header().Add("Trailer", "Livepeer-Settlement")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("data: hello\n\n"))
		w.Header().Set("Livepeer-Work-Units", "7")
		w.Header().Set("Livepeer-Settlement", base64.StdEncoding.EncodeToString(rawSettlement))
	}))
	defer broker.Close()
	loca := locOpenJob(t, broker.URL, "stream")
	defer loca.Close()

	client, _ := loc.NewClient(loc.Options{BaseURL: loca.URL, APIKey: apiKey})
	result, err := client.SubmitJob(context.Background(), loc.SubmitJobInput{
		Capability: "openai:chat-completions", Offering: "gpt-oss-20b",
		EstimatedUnits: 10, Body: []byte(`{"prompt":"hello"}`), Transport: "stream",
	})
	if err != nil {
		t.Fatal(err)
	}
	if result.ActualUnits != 7 || result.BodyText != "data: hello\n\n" {
		t.Fatalf("stream result: units=%d body=%q", result.ActualUnits, result.BodyText)
	}
	if result.RawHeaders.Get("Livepeer-Settlement") == "" {
		t.Fatal("signed settlement trailer was not retained")
	}
}

func TestSubmitJobMultipartAndTerminalError(t *testing.T) {
	t.Run("multipart", func(t *testing.T) {
		broker := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if got := r.Header.Get("Content-Type"); got != "multipart/form-data; boundary=boundary" {
				t.Fatalf("Content-Type = %q", got)
			}
			w.Header().Set("Content-Type", "application/json")
			w.Header().Set("Livepeer-Work-Units", "2")
			w.Header().Set("Livepeer-Work-Unit", "token")
			w.Header().Set("Livepeer-Job-Id", "broker-job-1")
			w.Header().Set("Livepeer-Settlement", encodedTestSettlement)
			_, _ = w.Write([]byte(`{"ok":true}`))
		}))
		defer broker.Close()
		loca := locOpenJob(t, broker.URL, "multipart")
		defer loca.Close()
		client, _ := loc.NewClient(loc.Options{BaseURL: loca.URL, APIKey: apiKey})
		result, err := client.SubmitJob(context.Background(), loc.SubmitJobInput{
			Capability: "x", Offering: "x", EstimatedUnits: 2,
			Body: []byte("--boundary--"), Transport: "multipart",
			ContentType: "multipart/form-data; boundary=boundary",
		})
		if err != nil || result.Transport != "multipart" {
			t.Fatalf("multipart result=%v err=%v", result, err)
		}
	})

	t.Run("terminal-error-zero", func(t *testing.T) {
		broker := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			w.Header().Set("Livepeer-Work-Units", "0")
			w.Header().Set("Livepeer-Work-Unit", "token")
			w.Header().Set("Livepeer-Job-Id", "broker-job-1")
			w.Header().Set("Livepeer-Settlement", encodedTestSettlement)
			w.WriteHeader(http.StatusTooManyRequests)
			_, _ = w.Write([]byte(`{"error":"rate_limited"}`))
		}))
		defer broker.Close()
		loca := locOpenJob(t, broker.URL, "unary")
		defer loca.Close()
		client, _ := loc.NewClient(loc.Options{BaseURL: loca.URL, APIKey: apiKey})
		result, err := client.SubmitJob(context.Background(), loc.SubmitJobInput{
			Capability: "x", Offering: "x", EstimatedUnits: 1, Body: []byte(`{}`),
		})
		if err != nil || result.Status != http.StatusTooManyRequests || result.ActualUnits != 0 {
			t.Fatalf("terminal error result=%v err=%v", result, err)
		}
	})
}

func TestSubmitJobRejectsWorkUnitDrift(t *testing.T) {
	broker := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Livepeer-Work-Units", "3")
		w.Header().Set("Livepeer-Work-Unit", "frames")
		w.Header().Set("Livepeer-Job-Id", "broker-job-1")
		w.Header().Set("Livepeer-Settlement", encodedTestSettlement)
		_, _ = w.Write([]byte(`{}`))
	}))
	defer broker.Close()
	loca := locOpenJob(t, broker.URL, "unary")
	defer loca.Close()
	client, _ := loc.NewClient(loc.Options{BaseURL: loca.URL, APIKey: apiKey})
	_, err := client.SubmitJob(context.Background(), loc.SubmitJobInput{
		Capability: "x", Offering: "x", EstimatedUnits: 3, Body: []byte(`{}`),
	})
	var protocolErr *loc.BrokerProtocolError
	if !errors.As(err, &protocolErr) || protocolErr.Code != "work_unit_mismatch" {
		t.Fatalf("expected work_unit_mismatch, got %v", err)
	}
}

func TestSubmitJobMapsInsufficientCredit(t *testing.T) {
	loca := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(402)
		_ = json.NewEncoder(w).Encode(map[string]any{
			"error": map[string]any{
				"code":    "INSUFFICIENT_CREDIT",
				"message": "broke",
				"details": map[string]any{"available_wei": "0", "required_wei": "1000"},
			},
		})
	}))
	defer loca.Close()

	client, _ := loc.NewClient(loc.Options{BaseURL: loca.URL, APIKey: apiKey})
	_, err := client.SubmitJob(context.Background(), loc.SubmitJobInput{
		Capability:     "x",
		Offering:       "x",
		EstimatedUnits: 1,
		Body:           []byte(`{}`),
	})
	if err == nil {
		t.Fatal("expected error")
	}
	apiErr, ok := err.(*loc.Error)
	if !ok {
		t.Fatalf("expected *Error, got %T", err)
	}
	if apiErr.Code != "INSUFFICIENT_CREDIT" {
		t.Errorf("code: got %q", apiErr.Code)
	}
	if apiErr.Status != 402 {
		t.Errorf("status: got %d", apiErr.Status)
	}
}

func TestOpenSession(t *testing.T) {
	loca := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(201)
		_ = json.NewEncoder(w).Encode(map[string]any{
			"session_id": "11111111-1111-1111-1111-111111111111",
			"work_id":    "wid-sess",
			"broker_url": "https://broker.example/livepeer",
			"request_id": "req-session",
			"protocol":   "paid-session/v1",
			"session": map[string]any{
				"descriptor_schema": "livepeer-session-test/v1", "attachment": "external",
				"metering": "runner-reported", "refill": "extensible",
			},
			"payment_envelope":   "BASE64SESS",
			"expected_value_wei": 100000,
			"funded_value_wei":   200000,
			"refill_endpoint":    "/v1/sessions/11111111-1111-1111-1111-111111111111/refill",
			"close_endpoint":     "/v1/sessions/11111111-1111-1111-1111-111111111111/close",
			"opened_at":          "2026-05-24T12:00:00Z",
		})
	}))
	defer loca.Close()

	client, _ := loc.NewClient(loc.Options{BaseURL: loca.URL, APIKey: apiKey})
	handle, err := client.OpenSession(context.Background(), loc.OpenSessionInput{
		Capability:           "livepeer:vtuber-session",
		Offering:             "vtuber-1080p30",
		DescriptorSchema:     "livepeer-session-test/v1",
		EstimatedRunwayUnits: 100,
		MaxTotalUnits:        200,
	})
	if err != nil {
		t.Fatal(err)
	}
	if handle.Protocol != "paid-session/v1" {
		t.Errorf("protocol: got %q", handle.Protocol)
	}
	if handle.FundedValueWei != 200000 {
		t.Errorf("funded: got %d", handle.FundedValueWei)
	}
}

func TestCloseSessionThreadsOutcome(t *testing.T) {
	var captured map[string]any
	loca := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&captured)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"session_id":       "22222222-2222-2222-2222-222222222222",
			"work_id":          "w",
			"actual_units":     100,
			"billed_value_wei": 100000,
			"refund_wei":       0,
			"outcome":          "EXACT",
			"closed_at":        "2026-05-24T12:30:00Z",
		})
	}))
	defer loca.Close()

	client, _ := loc.NewClient(loc.Options{BaseURL: loca.URL, APIKey: apiKey})
	_, err := client.CloseSession(context.Background(), "22222222-2222-2222-2222-222222222222", 100, "EXACT", map[string]any{"payload": map[string]any{}, "signature": map[string]any{}})
	if err != nil {
		t.Fatal(err)
	}
	if captured["actual_units"].(float64) != 100 {
		t.Errorf("actual_units: got %v", captured["actual_units"])
	}
	if captured["outcome"] != "EXACT" {
		t.Errorf("outcome: got %v", captured["outcome"])
	}
}

func TestListCapabilities(t *testing.T) {
	loca := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"items": []map[string]any{
				{"name": "openai:embeddings", "work_unit": "token", "offerings": []any{}},
			},
		})
	}))
	defer loca.Close()
	client, _ := loc.NewClient(loc.Options{BaseURL: loca.URL, APIKey: apiKey})
	caps, err := client.ListCapabilities(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(caps) != 1 || caps[0].Name != "openai:embeddings" {
		t.Errorf("got %+v", caps)
	}
}
