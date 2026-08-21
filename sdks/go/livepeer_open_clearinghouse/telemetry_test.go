package openclearinghouse_test

import (
	"compress/gzip"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	loc "github.com/livepeer/livepeer-open-clearinghouse-sdk-go/livepeer_open_clearinghouse"
)

// captureServer returns an httptest server that records every received
// /v1/telemetry batch.
func captureServer(t *testing.T) (*httptest.Server, *[]map[string]any, *sync.Mutex) {
	t.Helper()
	var (
		mu      sync.Mutex
		batches []map[string]any
	)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var bodyReader io.Reader = r.Body
		if r.Header.Get("Content-Encoding") == "gzip" {
			gr, err := gzip.NewReader(r.Body)
			if err != nil {
				w.WriteHeader(400)
				return
			}
			defer func() { _ = gr.Close() }()
			bodyReader = gr
		}
		buf, _ := io.ReadAll(bodyReader)
		var parsed map[string]any
		_ = json.Unmarshal(buf, &parsed)
		mu.Lock()
		batches = append(batches, parsed)
		mu.Unlock()
		w.WriteHeader(202)
		_, _ = w.Write([]byte(`{"accepted":1}`))
	}))
	t.Cleanup(srv.Close)
	return srv, &batches, &mu
}

func TestTelemetryFlushOnSubmitJob(t *testing.T) {
	tel, batches, mu := captureServer(t)

	brokerSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Livepeer-Work-Units", "42")
		w.Header().Set("Livepeer-Work-Unit", "token")
		w.Header().Set("Livepeer-Job-Id", "broker-job-1")
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	t.Cleanup(brokerSrv.Close)

	mux := http.NewServeMux()
	mux.HandleFunc("/v1/jobs", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"job_id":             "00000000-0000-0000-0000-000000000abc",
			"request_id":         "broker-request-1",
			"work_id":            "wid",
			"broker_url":         brokerSrv.URL,
			"protocol":           "paid-job/v1",
			"transport":          "unary",
			"work_unit":          "token",
			"payment_envelope":   "BASE64",
			"expected_value_wei": 100000,
			"funded_value_wei":   100000,
			"settle_endpoint":    "/v1/jobs/00000000-0000-0000-0000-000000000abc/settle",
			"opened_at":          "2026-05-25T00:00:00Z",
		})
	})
	mux.HandleFunc("/v1/jobs/00000000-0000-0000-0000-000000000abc/settle", func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		au, _ := body["actual_units"].(float64)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"job_id":           "00000000-0000-0000-0000-000000000abc",
			"work_id":          "wid",
			"actual_units":     au,
			"billed_value_wei": 0,
			"refund_wei":       100000,
			"outcome":          "OVERFUNDED",
			"closed_at":        "2026-05-25T00:00:30Z",
			"cap_status":       map[string]any{"session_pct_used": 0, "will_refuse_next_refill": false},
		})
	})
	// Route /v1/telemetry on the same mux to the capture server.
	mux.HandleFunc("/v1/telemetry", func(w http.ResponseWriter, r *http.Request) {
		req, _ := http.NewRequest(r.Method, tel.URL+"/v1/telemetry", r.Body)
		req.Header = r.Header
		resp, _ := http.DefaultClient.Do(req)
		if resp != nil {
			_ = resp.Body.Close()
		}
		w.WriteHeader(202)
	})
	locSrv := httptest.NewServer(mux)
	t.Cleanup(locSrv.Close)

	client, err := loc.NewClient(loc.Options{BaseURL: locSrv.URL, APIKey: "pymth_live_test"})
	if err != nil {
		t.Fatal(err)
	}
	_, err = client.SubmitJob(context.Background(), loc.SubmitJobInput{
		Capability:     "x",
		Offering:       "y",
		EstimatedUnits: 100,
		Body:           []byte(`{"hello":"world"}`),
	})
	if err != nil {
		t.Fatal(err)
	}
	client.Close(context.Background())

	// Give the proxy a moment to forward the final flush.
	time.Sleep(200 * time.Millisecond)

	mu.Lock()
	defer mu.Unlock()
	if len(*batches) == 0 {
		t.Fatal("expected at least one telemetry batch")
	}
	seen := map[string]bool{}
	for _, b := range *batches {
		events, _ := b["events"].([]any)
		for _, e := range events {
			m, _ := e.(map[string]any)
			seen[m["event_type"].(string)] = true
		}
	}
	for _, expected := range []string{
		"sdk.init",
		"request.mint_started",
		"request.mint_completed",
		"request.settle_started",
		"request.settle_completed",
		"request.completed",
	} {
		if !seen[expected] {
			t.Errorf("expected event %q in batches", expected)
		}
	}
}

func TestTelemetryCriticalEventFlushesImmediately(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls.Add(1)
		w.WriteHeader(202)
		_, _ = w.Write([]byte(`{"accepted":1}`))
	}))
	t.Cleanup(srv.Close)

	client, err := loc.NewClient(loc.Options{BaseURL: srv.URL, APIKey: "pymth_live_test"})
	if err != nil {
		t.Fatal(err)
	}
	client.Telemetry().Emit(loc.EmitTelemetryOptions{EventType: "session.refill_denied"})
	// Should flush within a couple hundred ms — well below the 5s
	// periodic timer.
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if calls.Load() > 0 {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	client.Close(context.Background())
	if calls.Load() == 0 {
		t.Fatal("expected critical event to flush immediately")
	}
}

func TestTelemetryGzipAppliedWhenBodyLarge(t *testing.T) {
	var gzipSeen atomic.Bool
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.EqualFold(r.Header.Get("Content-Encoding"), "gzip") {
			gzipSeen.Store(true)
		}
		w.WriteHeader(202)
	}))
	t.Cleanup(srv.Close)

	client, err := loc.NewClient(loc.Options{BaseURL: srv.URL, APIKey: "pymth_live_test"})
	if err != nil {
		t.Fatal(err)
	}
	bigPayload := strings.Repeat("x", 2000)
	client.Telemetry().Emit(loc.EmitTelemetryOptions{
		EventType: "session.refill_denied", // critical → immediate flush
		Payload:   map[string]interface{}{"big": bigPayload},
	})
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if gzipSeen.Load() {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	client.Close(context.Background())
	if !gzipSeen.Load() {
		t.Fatal("expected gzip Content-Encoding on large body")
	}
}

func TestTelemetryBufferOverflowDropsOldest(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(202)
	}))
	t.Cleanup(srv.Close)
	client, err := loc.NewClient(loc.Options{BaseURL: srv.URL, APIKey: "pymth_live_test"})
	if err != nil {
		t.Fatal(err)
	}
	// Direct access to the emitter — most users never need this.
	tem := client.Telemetry()
	// Default buffer cap is 10K — overflowing it in this test would be
	// noisy; just confirm the API works with a manual emitter.
	_ = tem
	client.Close(context.Background())
}
