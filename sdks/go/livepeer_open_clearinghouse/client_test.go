// Tests for the handoff-mode Go SDK. Uses net/http/httptest for both
// the LOC gateway and the broker.
package openclearinghouse_test

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"

	loc "github.com/livepeer/livepeer-open-clearinghouse-sdk-go/livepeer_open_clearinghouse"
)

const apiKey = "pymth_live_test"
const encodedTestSettlement = "eyJwYXlsb2FkIjp7fSwic2lnbmF0dXJlIjp7fX0="

func callerProofInput(in loc.SubmitJobInput) loc.SubmitJobInput {
	in.CallerPublicKey = "02" + strings.Repeat("11", 32)
	in.SignCallerProof = func([]byte) (string, error) { return "CALLER-PROOF", nil }
	return in
}

func locOpenJob(t *testing.T, brokerURL, transport string) *httptest.Server {
	t.Helper()
	return locOpenJobWithRoute(t, brokerURL, transport, nil)
}

// locOpenJobWithRoute is locOpenJob with a route_snapshot on the open
// response (v2 gateways always send one; nil omits it).
func locOpenJobWithRoute(t *testing.T, brokerURL, transport string, routeSnapshot map[string]any) *httptest.Server {
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
		opened := map[string]any{
			"job_id":              "00000000-0000-0000-0000-000000000abc",
			"request_id":          "broker-request-1",
			"work_id":             "wid-abc",
			"broker_url":          brokerURL,
			"protocol":            "paid-job/v1",
			"transport":           transport,
			"work_unit":           "token",
			"spend_authorization": base64.StdEncoding.EncodeToString([]byte("authorization")),
			"accounting_mode":     "wholesale_account",
			"expected_value_wei":  "100000",
			"funded_value_wei":    "100000",
			"settle_endpoint":     "/v1/jobs/00000000-0000-0000-0000-000000000abc/settle",
			"opened_at":           "2026-05-24T12:00:00Z",
		}
		if routeSnapshot != nil {
			opened["route_snapshot"] = routeSnapshot
		}
		_ = json.NewEncoder(w).Encode(opened)
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
			"billed_value_wei": strconv.FormatInt(int64(au)*1000, 10),
			"refund_wei":       strconv.FormatInt(100000-int64(au)*1000, 10),
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
		if got := r.Header.Get("Livepeer-Payment"); got != "" {
			t.Errorf("unexpected Livepeer-Payment %q", got)
		}
		if r.Header.Get("Livepeer-Authorization") == "" || r.Header.Get("Livepeer-Caller-Proof") != "CALLER-PROOF" {
			t.Errorf("missing wholesale authorization headers: %v", r.Header)
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
	result, err := client.SubmitJob(context.Background(), callerProofInput(loc.SubmitJobInput{
		Capability:     "openai:chat-completions",
		Offering:       "gpt-oss-20b",
		EstimatedUnits: 80,
		MaxTotalUnits:  100,
		Body:           []byte(`{"prompt":"hello"}`),
	}))
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != 200 {
		t.Errorf("status: got %d, want 200", result.Status)
	}
	if result.ActualUnits != 42 {
		t.Errorf("actual_units: got %d, want 42", result.ActualUnits)
	}
	if result.BilledValueWei.Cmp(loc.NewWei(42000)) != 0 {
		t.Errorf("billed: got %s, want 42000", result.BilledValueWei)
	}
	if result.RefundWei.Cmp(loc.NewWei(58000)) != 0 {
		t.Errorf("refund: got %s, want 58000", result.RefundWei)
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
	result, err := client.SubmitJob(context.Background(), callerProofInput(loc.SubmitJobInput{
		Capability: "openai:chat-completions", Offering: "gpt-oss-20b",
		EstimatedUnits: 10, Body: []byte(`{"prompt":"hello"}`), Transport: "stream",
	}))
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
		result, err := client.SubmitJob(context.Background(), callerProofInput(loc.SubmitJobInput{
			Capability: "x", Offering: "x", EstimatedUnits: 2,
			Body: []byte("--boundary--"), Transport: "multipart",
			ContentType: "multipart/form-data; boundary=boundary",
		}))
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
		result, err := client.SubmitJob(context.Background(), callerProofInput(loc.SubmitJobInput{
			Capability: "x", Offering: "x", EstimatedUnits: 1, Body: []byte(`{}`),
		}))
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
	_, err := client.SubmitJob(context.Background(), callerProofInput(loc.SubmitJobInput{
		Capability: "x", Offering: "x", EstimatedUnits: 3, Body: []byte(`{}`),
	}))
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
	_, err := client.SubmitJob(context.Background(), callerProofInput(loc.SubmitJobInput{
		Capability:     "x",
		Offering:       "x",
		EstimatedUnits: 1,
		Body:           []byte(`{}`),
	}))
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
		if r.URL.Path == "/v1/sessions/prepare" {
			_ = json.NewEncoder(w).Encode(map[string]any{
				"gateway_session_id": "11111111-1111-1111-1111-111111111111",
				"route_binding":      map[string]any{}, "preparation_token": "prepared-token",
			})
			return
		}
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
			"spend_authorization": base64.StdEncoding.EncodeToString([]byte("session-authorization")),
			"accounting_mode":     "wholesale_account",
			"expected_value_wei":  "100000",
			"funded_value_wei":    "200000",
			"refill_endpoint":     "/v1/sessions/11111111-1111-1111-1111-111111111111/refill",
			"close_endpoint":      "/v1/sessions/11111111-1111-1111-1111-111111111111/close",
			"opened_at":           "2026-05-24T12:00:00Z",
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
		CallerPublicKey:      "02" + strings.Repeat("11", 32),
		SignCallerProof:      func([]byte) (string, error) { return "CALLER-PROOF", nil },
	})
	if err != nil {
		t.Fatal(err)
	}
	if handle.Protocol != "paid-session/v1" {
		t.Errorf("protocol: got %q", handle.Protocol)
	}
	if handle.FundedValueWei.Cmp(loc.NewWei(200000)) != 0 {
		t.Errorf("funded: got %s", handle.FundedValueWei)
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
			"billed_value_wei": "100000",
			"refund_wei":       "0",
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

// bodyCapturingBroker is a happy-path broker that records the raw request
// body it received, so tests can assert what SubmitJob actually sent.
func bodyCapturingBroker(t *testing.T) (*httptest.Server, *[]byte) {
	t.Helper()
	var captured []byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		captured, _ = io.ReadAll(r.Body)
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Livepeer-Work-Units", "1")
		w.Header().Set("Livepeer-Work-Unit", "token")
		w.Header().Set("Livepeer-Job-Id", "broker-job-1")
		w.Header().Set("Livepeer-Settlement", encodedTestSettlement)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	t.Cleanup(srv.Close)
	return srv, &captured
}

func TestSubmitJobInjectsRouteModel(t *testing.T) {
	routeWithModel := map[string]any{
		"broker_url": "http://ignored",
		"extra":      map[string]any{"openai": map[string]any{"model": "qwen/qwen3.6-27b"}},
	}
	submit := func(t *testing.T, routeSnapshot map[string]any, in loc.SubmitJobInput) []byte {
		t.Helper()
		broker, captured := bodyCapturingBroker(t)
		loca := locOpenJobWithRoute(t, broker.URL, "unary", routeSnapshot)
		t.Cleanup(loca.Close)
		client, err := loc.NewClient(loc.Options{BaseURL: loca.URL, APIKey: apiKey})
		if err != nil {
			t.Fatal(err)
		}
		if _, err := client.SubmitJob(context.Background(), callerProofInput(in)); err != nil {
			t.Fatalf("SubmitJob: %v", err)
		}
		return *captured
	}
	decode := func(t *testing.T, raw []byte) map[string]any {
		t.Helper()
		var out map[string]any
		if err := json.Unmarshal(raw, &out); err != nil {
			t.Fatalf("broker body is not a JSON object: %q", raw)
		}
		return out
	}

	t.Run("does not mutate an authorized body", func(t *testing.T) {
		got := decode(t, submit(t, routeWithModel, loc.SubmitJobInput{
			Capability: "openai:chat-completions", Offering: "qwen3.6-27b", EstimatedUnits: 1,
			Body: []byte(`{"messages":[{"role":"user","content":"hi"}],"max_tokens":48}`),
		}))
		if got["model"] != nil {
			t.Fatalf("authorized body was mutated with model %v", got["model"])
		}
		if got["max_tokens"] != float64(48) || got["messages"] == nil {
			t.Fatalf("other fields not preserved: %v", got)
		}
	})

	t.Run("caller-supplied model untouched", func(t *testing.T) {
		body := []byte(`{"model":"mine","messages":[]}`)
		raw := submit(t, routeWithModel, loc.SubmitJobInput{
			Capability: "openai:chat-completions", Offering: "x", EstimatedUnits: 1, Body: body,
		})
		if string(raw) != string(body) {
			t.Fatalf("body rewritten: %s", raw)
		}
	})

	t.Run("non-openai capability untouched", func(t *testing.T) {
		body := []byte(`{"schema":"video-transcode-vod/v2"}`)
		raw := submit(t, routeWithModel, loc.SubmitJobInput{
			Capability: "video:transcode.vod", Offering: "vod-default", EstimatedUnits: 1, Body: body,
		})
		if string(raw) != string(body) {
			t.Fatalf("body rewritten: %s", raw)
		}
	})

	t.Run("non-JSON body untouched", func(t *testing.T) {
		body := []byte("plain text prompt")
		raw := submit(t, routeWithModel, loc.SubmitJobInput{
			Capability: "openai:chat-completions", Offering: "x", EstimatedUnits: 1, Body: body,
		})
		if string(raw) != string(body) {
			t.Fatalf("body rewritten: %s", raw)
		}
	})

	t.Run("JSON array body untouched", func(t *testing.T) {
		body := []byte(`[{"role":"user"}]`)
		raw := submit(t, routeWithModel, loc.SubmitJobInput{
			Capability: "openai:chat-completions", Offering: "x", EstimatedUnits: 1, Body: body,
		})
		if string(raw) != string(body) {
			t.Fatalf("body rewritten: %s", raw)
		}
	})

	t.Run("route without model untouched", func(t *testing.T) {
		body := []byte(`{"messages":[]}`)
		raw := submit(t, map[string]any{"extra": map[string]any{}}, loc.SubmitJobInput{
			Capability: "openai:chat-completions", Offering: "x", EstimatedUnits: 1, Body: body,
		})
		if string(raw) != string(body) {
			t.Fatalf("body rewritten: %s", raw)
		}
	})
}

// TestErrorMapsFastAPIValidationBody covers the 422 shape FastAPI emits
// when a request fails schema validation: `detail` is a list, not a
// string. The mapper must produce a typed *Error with an empty Code, the
// compact JSON of detail as Message, and the decoded detail in Details.
func TestErrorMapsFastAPIValidationBody(t *testing.T) {
	serve := func(t *testing.T, payload string) error {
		t.Helper()
		loca := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(422)
			_, _ = w.Write([]byte(payload))
		}))
		t.Cleanup(loca.Close)
		client, _ := loc.NewClient(loc.Options{BaseURL: loca.URL, APIKey: apiKey})
		_, err := client.SubmitJob(context.Background(), callerProofInput(loc.SubmitJobInput{
			Capability: "x", Offering: "x", EstimatedUnits: 1, Body: []byte(`{}`),
		}))
		return err
	}

	t.Run("detail list", func(t *testing.T) {
		err := serve(t, `{"detail":[{"type":"missing","loc":["body","settlement","signature"],"msg":"Field required"}]}`)
		var apiErr *loc.Error
		if !errors.As(err, &apiErr) {
			t.Fatalf("expected *Error, got %T: %v", err, err)
		}
		if apiErr.Code != "" {
			t.Errorf("code = %q; want empty", apiErr.Code)
		}
		if apiErr.Status != 422 {
			t.Errorf("status = %d", apiErr.Status)
		}
		want := `[{"loc":["body","settlement","signature"],"msg":"Field required","type":"missing"}]`
		if apiErr.Message != want {
			t.Errorf("message = %q; want %q", apiErr.Message, want)
		}
		list, ok := apiErr.Details["detail"].([]any)
		if !ok || len(list) != 1 {
			t.Fatalf("details.detail = %#v; want the decoded list", apiErr.Details["detail"])
		}
		if first, _ := list[0].(map[string]any); first["msg"] != "Field required" {
			t.Errorf("details.detail[0] = %v", list[0])
		}
	})

	t.Run("detail object", func(t *testing.T) {
		err := serve(t, `{"detail":{"reason":"missing_signature"}}`)
		var apiErr *loc.Error
		if !errors.As(err, &apiErr) {
			t.Fatalf("expected *Error, got %T: %v", err, err)
		}
		if apiErr.Code != "" || apiErr.Message != `{"reason":"missing_signature"}` {
			t.Errorf("code=%q message=%q", apiErr.Code, apiErr.Message)
		}
		if obj, _ := apiErr.Details["detail"].(map[string]any); obj["reason"] != "missing_signature" {
			t.Errorf("details.detail = %#v", apiErr.Details["detail"])
		}
	})

	t.Run("detail string still maps to code", func(t *testing.T) {
		err := serve(t, `{"detail":"Not Found"}`)
		var apiErr *loc.Error
		if !errors.As(err, &apiErr) || apiErr.Code != "Not Found" || apiErr.Message != "Not Found" {
			t.Fatalf("unexpected: %v", err)
		}
	})

	t.Run("long detail truncated to 500 chars", func(t *testing.T) {
		items := make([]string, 0, 40)
		for i := 0; i < 40; i++ {
			items = append(items, `{"type":"missing","loc":["body","field"],"msg":"Field required"}`)
		}
		err := serve(t, `{"detail":[`+strings.Join(items, ",")+`]}`)
		var apiErr *loc.Error
		if !errors.As(err, &apiErr) {
			t.Fatalf("expected *Error, got %T", err)
		}
		if n := len([]rune(apiErr.Message)); n != 500 || !strings.HasSuffix(apiErr.Message, "...") {
			t.Errorf("message length = %d, suffix ok = %t; want 500 chars ending in ...", n, strings.HasSuffix(apiErr.Message, "..."))
		}
		if list, _ := apiErr.Details["detail"].([]any); len(list) != 40 {
			t.Errorf("details.detail should keep the full list, got %d items", len(list))
		}
	})
}

// exchangeBroker serves a stream POST /v1/job whose terminal claim is NOT
// on the response (as when a proxy strips trailers) and answers
// GET /v1/exchange/{request_id} from the supplied responder, which is
// called with the 1-based attempt number.
func exchangeBroker(t *testing.T, responder func(attempt int) (int, map[string]any)) (*httptest.Server, *int) {
	t.Helper()
	attempts := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/v1/job":
			w.Header().Set("Content-Type", "text/event-stream")
			w.Header().Set("Livepeer-Job-Id", "broker-job-1")
			w.Header().Set("Livepeer-Request-Id", "broker-request-1")
			w.Header().Set("Livepeer-Work-Unit", "token")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte("data: hello\n\ndata: [DONE]\n\n"))
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/v1/exchange/"):
			attempts++
			status, body := responder(attempts)
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(status)
			_ = json.NewEncoder(w).Encode(body)
		default:
			t.Errorf("unexpected broker request %s %s", r.Method, r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	return srv, &attempts
}

func settledExchange() map[string]any {
	return map[string]any{
		"request_id": "broker-request-1",
		"job_id":     "broker-job-1",
		"outcome":    "SETTLED",
		"work_units": 7,
		"unit":       "token",
		"settlement": encodedTestSettlement,
	}
}

func submitStream(t *testing.T, brokerURL string) (*loc.JobResult, error) {
	t.Helper()
	loca := locOpenJob(t, brokerURL, "stream")
	t.Cleanup(loca.Close)
	client, err := loc.NewClient(loc.Options{BaseURL: loca.URL, APIKey: apiKey})
	if err != nil {
		t.Fatal(err)
	}
	return client.SubmitJob(context.Background(), callerProofInput(loc.SubmitJobInput{
		Capability: "openai:chat-completions", Offering: "x", EstimatedUnits: 10,
		Body: []byte(`{"messages":[]}`), Transport: "stream",
	}))
}

func TestSubmitJobRecoversClaimViaExchange(t *testing.T) {
	t.Run("stream without trailers settles via exchange", func(t *testing.T) {
		broker, attempts := exchangeBroker(t, func(int) (int, map[string]any) { return 200, settledExchange() })
		result, err := submitStream(t, broker.URL)
		if err != nil {
			t.Fatalf("SubmitJob: %v", err)
		}
		if *attempts != 1 {
			t.Errorf("exchange attempts = %d; want 1", *attempts)
		}
		if result.ActualUnits != 7 || result.BrokerJobID != "broker-job-1" || result.WorkUnit != "token" {
			t.Fatalf("claim: units=%d job=%q unit=%q", result.ActualUnits, result.BrokerJobID, result.WorkUnit)
		}
		if result.Status != 200 || !strings.HasPrefix(result.BodyText, "data: hello") {
			t.Fatalf("broker response not preserved: status=%d body=%q", result.Status, result.BodyText)
		}
	})

	t.Run("exchange pending then settled", func(t *testing.T) {
		broker, attempts := exchangeBroker(t, func(attempt int) (int, map[string]any) {
			switch attempt {
			case 1:
				return 202, map[string]any{"request_id": "broker-request-1", "outcome": "IN_FLIGHT"}
			case 2:
				return 202, map[string]any{"request_id": "broker-request-1", "outcome": "ACCOUNTING_PENDING"}
			}
			return 200, settledExchange()
		})
		result, err := submitStream(t, broker.URL)
		if err != nil {
			t.Fatalf("SubmitJob: %v", err)
		}
		if *attempts != 3 {
			t.Errorf("exchange attempts = %d; want 3", *attempts)
		}
		if result.ActualUnits != 7 {
			t.Errorf("actual_units = %d", result.ActualUnits)
		}
	})

	t.Run("mismatched request id rejected", func(t *testing.T) {
		broker, _ := exchangeBroker(t, func(int) (int, map[string]any) {
			body := settledExchange()
			body["request_id"] = "someone-else"
			return 200, body
		})
		_, err := submitStream(t, broker.URL)
		var protoErr *loc.BrokerProtocolError
		if !errors.As(err, &protoErr) || protoErr.Code != "broker_request_id_mismatch" {
			t.Fatalf("expected broker_request_id_mismatch, got %v", err)
		}
	})

	t.Run("mismatched job id rejected", func(t *testing.T) {
		broker, _ := exchangeBroker(t, func(int) (int, map[string]any) {
			body := settledExchange()
			body["job_id"] = "broker-job-2"
			return 200, body
		})
		_, err := submitStream(t, broker.URL)
		var protoErr *loc.BrokerProtocolError
		if !errors.As(err, &protoErr) || protoErr.Code != "broker_job_id_mismatch" {
			t.Fatalf("expected broker_job_id_mismatch, got %v", err)
		}
	})

	t.Run("unresolved outcome rejected", func(t *testing.T) {
		broker, _ := exchangeBroker(t, func(int) (int, map[string]any) {
			return 200, map[string]any{"request_id": "broker-request-1", "outcome": "FAILED"}
		})
		_, err := submitStream(t, broker.URL)
		var protoErr *loc.BrokerProtocolError
		if !errors.As(err, &protoErr) || protoErr.Code != "broker_exchange_unresolved" {
			t.Fatalf("expected broker_exchange_unresolved, got %v", err)
		}
	})

	t.Run("still pending after retries", func(t *testing.T) {
		broker, attempts := exchangeBroker(t, func(int) (int, map[string]any) {
			return 202, map[string]any{"request_id": "broker-request-1", "outcome": "IN_FLIGHT"}
		})
		_, err := submitStream(t, broker.URL)
		var protoErr *loc.BrokerProtocolError
		if !errors.As(err, &protoErr) || protoErr.Code != "broker_exchange_pending" {
			t.Fatalf("expected broker_exchange_pending, got %v", err)
		}
		if *attempts != 8 {
			t.Errorf("exchange attempts = %d; want 8", *attempts)
		}
	})

	t.Run("stream response without Livepeer-Job-Id rejected before lookup", func(t *testing.T) {
		broker := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path != "/v1/job" {
				t.Errorf("unexpected lookup %s", r.URL.Path)
			}
			w.Header().Set("Content-Type", "text/event-stream")
			_, _ = w.Write([]byte("data: hello\n\n"))
		}))
		t.Cleanup(broker.Close)
		_, err := submitStream(t, broker.URL)
		var protoErr *loc.BrokerProtocolError
		if !errors.As(err, &protoErr) || protoErr.Code != "broker_protocol_error" {
			t.Fatalf("expected broker_protocol_error, got %v", err)
		}
		if missing, _ := protoErr.Details["missing_headers"].([]string); len(missing) != 1 || missing[0] != "Livepeer-Job-Id" {
			t.Errorf("details.missing_headers = %v", protoErr.Details["missing_headers"])
		}
	})

	t.Run("settlement header and body disagree rejected", func(t *testing.T) {
		broker := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path == "/v1/job" {
				w.Header().Set("Content-Type", "text/event-stream")
				w.Header().Set("Livepeer-Job-Id", "broker-job-1")
				_, _ = w.Write([]byte("data: hello\n\n"))
				return
			}
			w.Header().Set("Content-Type", "application/json")
			w.Header().Set("Livepeer-Settlement", "eyJwYXlsb2FkIjp7ImEiOjF9fQ==")
			body := settledExchange()
			_ = json.NewEncoder(w).Encode(body)
		}))
		t.Cleanup(broker.Close)
		_, err := submitStream(t, broker.URL)
		var protoErr *loc.BrokerProtocolError
		if !errors.As(err, &protoErr) || protoErr.Code != "broker_protocol_error" || !strings.Contains(protoErr.Message, "disagree") {
			t.Fatalf("expected settlement disagreement error, got %v", err)
		}
	})
}
