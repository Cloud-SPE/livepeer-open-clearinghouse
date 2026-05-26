package openclearinghouse_test

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"

	loc "github.com/livepeer/livepeer-open-clearinghouse-sdk-go/livepeer_open_clearinghouse"
)

// wsServer spins up an in-process WebSocket server that runs the
// supplied handler for every connection.
func wsServer(t *testing.T, handler func(ctx context.Context, conn *websocket.Conn)) (url string, stop func()) {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{
			InsecureSkipVerify: true,
		})
		if err != nil {
			t.Errorf("ws accept: %v", err)
			return
		}
		defer func() { _ = conn.Close(websocket.StatusNormalClosure, "test done") }()
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		handler(ctx, conn)
	}))
	wsURL := strings.Replace(srv.URL, "http://", "ws://", 1)
	return wsURL, srv.Close
}

func locRefillServer(t *testing.T, sid string) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/sessions/"+sid+"/refill", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"work_id":            "wid",
			"refill_seq":         float64(1),
			"payment_envelope":   "REFILL-ENV",
			"expected_value_wei": float64(50000),
			"funded_value_wei":   float64(50000),
			"cap_status": map[string]any{
				"session_pct_used":        float64(0.4),
				"spend_period_pct_used":   nil,
				"user_balance_pct_used":   nil,
				"operator_pool_pct_used":  nil,
				"will_refuse_next_refill": false,
				"winddown_reason":         nil,
			},
		})
	})
	mux.HandleFunc("/v1/sessions/"+sid+"/close", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"session_id":       sid,
			"work_id":          "wid",
			"actual_units":     float64(80),
			"billed_value_wei": float64(80000),
			"refund_wei":       float64(120000),
			"outcome":          "OVERFUNDED",
			"closed_at":        "2026-05-24T12:30:00Z",
		})
	})
	return httptest.NewServer(mux)
}

func locRefuseServer(t *testing.T, sid string) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/sessions/"+sid+"/refill", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(402)
		_, _ = w.Write([]byte(`{"error":{"code":"cap_reached","message":"period cap reached","details":{"which":"spend_period"}}}`))
	})
	mux.HandleFunc("/v1/sessions/"+sid+"/close", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"session_id":       sid,
			"work_id":          "wid",
			"actual_units":     float64(0),
			"billed_value_wei": float64(0),
			"refund_wei":       float64(200000),
			"outcome":          "OVERFUNDED",
			"closed_at":        "2026-05-24T12:30:00Z",
		})
	})
	return httptest.NewServer(mux)
}

func TestSessionRunnerRefillsOnBalanceLow(t *testing.T) {
	sid := "11111111-1111-1111-1111-111111111111"
	loca := locRefillServer(t, sid)
	defer loca.Close()

	var (
		receivedFrames []string
		mu             sync.Mutex
	)
	done := make(chan struct{}, 1)
	wsURL, stop := wsServer(t, func(ctx context.Context, conn *websocket.Conn) {
		_ = conn.Write(ctx, websocket.MessageText,
			[]byte(`{"type":"session.balance.low","observed_consumed_units":80}`))
		for {
			_, msg, err := conn.Read(ctx)
			if err != nil {
				return
			}
			mu.Lock()
			receivedFrames = append(receivedFrames, string(msg))
			mu.Unlock()
			select {
			case done <- struct{}{}:
			default:
			}
		}
	})
	defer stop()

	client, err := loc.NewClient(loc.Options{BaseURL: loca.URL, APIKey: apiKey})
	if err != nil {
		t.Fatal(err)
	}

	var (
		refillEvent loc.RefillEvent
		gotRefill   bool
		evtMu       sync.Mutex
	)
	runner := loc.NewSessionRunner(loc.SessionRunnerOptions{
		Client: client,
		Handle: &loc.SessionHandle{
			SessionID:       sid,
			BrokerURL:       wsURL,
			Mode:            "session-control-plus-media@v0",
			PaymentEnvelope: "BASE64ENV",
		},
		OnRefillSucceeded: func(e loc.RefillEvent) {
			evtMu.Lock()
			refillEvent = e
			gotRefill = true
			evtMu.Unlock()
		},
	})
	if err := runner.Start(context.Background()); err != nil {
		t.Fatalf("start: %v", err)
	}
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for topup frame")
	}
	if _, err := runner.Close(context.Background(), loc.CloseSessionInput{ActualUnits: 0}); err != nil {
		t.Fatalf("close: %v", err)
	}

	evtMu.Lock()
	defer evtMu.Unlock()
	if !gotRefill {
		t.Fatal("onRefillSucceeded was not invoked")
	}
	if refillEvent.RefillSeq == nil || *refillEvent.RefillSeq != 1 {
		t.Errorf("refill_seq mismatch: %+v", refillEvent.RefillSeq)
	}

	mu.Lock()
	defer mu.Unlock()
	if len(receivedFrames) != 1 {
		t.Fatalf("expected 1 frame, got %d", len(receivedFrames))
	}
	var frame map[string]any
	if err := json.Unmarshal([]byte(receivedFrames[0]), &frame); err != nil {
		t.Fatalf("frame parse: %v", err)
	}
	if frame["type"] != "session.topup" {
		t.Errorf("frame.type: %v", frame["type"])
	}
	body, _ := frame["body"].(map[string]any)
	if body["payment_header"] != "REFILL-ENV" {
		t.Errorf("payment_header: %v", body["payment_header"])
	}
}

func TestSessionRunnerWSRealtimeBoundedFiresWinddownOnly(t *testing.T) {
	sid := "22222222-2222-2222-2222-222222222222"
	refillCalled := false
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/sessions/"+sid+"/refill", func(w http.ResponseWriter, r *http.Request) {
		refillCalled = true
		w.WriteHeader(400)
	})
	mux.HandleFunc("/v1/sessions/"+sid+"/close", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"session_id": sid, "outcome": "OVERFUNDED",
			"actual_units": float64(0), "billed_value_wei": float64(0),
			"refund_wei": float64(200000), "closed_at": "2026-05-24T12:30:00Z",
		})
	})
	loca := httptest.NewServer(mux)
	defer loca.Close()

	wsURL, stop := wsServer(t, func(ctx context.Context, conn *websocket.Conn) {
		_ = conn.Write(ctx, websocket.MessageText,
			[]byte(`{"type":"session.balance.low"}`))
		// Idle until client closes us
		_, _, _ = conn.Read(ctx)
	})
	defer stop()

	client, _ := loc.NewClient(loc.Options{BaseURL: loca.URL, APIKey: apiKey})

	var (
		wd     loc.WinddownEvent
		gotWd  bool
		wdMu   sync.Mutex
		wdSig  = make(chan struct{}, 1)
	)
	runner := loc.NewSessionRunner(loc.SessionRunnerOptions{
		Client: client,
		Handle: &loc.SessionHandle{
			SessionID:       sid,
			BrokerURL:       wsURL,
			Mode:            "ws-realtime@v0",
			PaymentEnvelope: "BASE64ENV",
		},
		OnWinddownWarning: func(e loc.WinddownEvent) {
			wdMu.Lock()
			wd = e
			gotWd = true
			wdMu.Unlock()
			select {
			case wdSig <- struct{}{}:
			default:
			}
		},
	})
	if err := runner.Start(context.Background()); err != nil {
		t.Fatalf("start: %v", err)
	}
	select {
	case <-wdSig:
	case <-time.After(2 * time.Second):
		t.Fatal("did not see winddown event")
	}
	_, _ = runner.Close(context.Background(), loc.CloseSessionInput{ActualUnits: 0})

	wdMu.Lock()
	defer wdMu.Unlock()
	if !gotWd {
		t.Fatal("expected winddown callback")
	}
	if wd.Reason != "ws_session_exhausting" {
		t.Errorf("winddown reason: %q", wd.Reason)
	}
	if refillCalled {
		t.Error("ws-realtime must NOT call refill")
	}
}

func TestSessionRunnerUnsupportedModeRaises(t *testing.T) {
	loca := httptest.NewServer(http.NewServeMux())
	defer loca.Close()
	client, _ := loc.NewClient(loc.Options{BaseURL: loca.URL, APIKey: apiKey})
	runner := loc.NewSessionRunner(loc.SessionRunnerOptions{
		Client: client,
		Handle: &loc.SessionHandle{
			SessionID:       "00000000-0000-0000-0000-000000000444",
			BrokerURL:       "http://broker.test",
			Mode:            "http-reqresp@v0",
			PaymentEnvelope: "BASE64ENV",
		},
	})
	if err := runner.Start(context.Background()); err == nil {
		t.Fatal("expected unsupported mode error")
	} else if !strings.Contains(err.Error(), "unsupported mode") {
		t.Errorf("unexpected error %v", err)
	}
}

func TestSessionRunnerRefillRefused(t *testing.T) {
	sid := "33333333-3333-3333-3333-333333333333"
	loca := locRefuseServer(t, sid)
	defer loca.Close()

	wsURL, stop := wsServer(t, func(ctx context.Context, conn *websocket.Conn) {
		_ = conn.Write(ctx, websocket.MessageText,
			[]byte(`{"type":"session.balance.low"}`))
		_, _, _ = conn.Read(ctx)
	})
	defer stop()

	client, _ := loc.NewClient(loc.Options{BaseURL: loca.URL, APIKey: apiKey})

	var (
		refused   loc.RefillEvent
		gotRef    bool
		refMu     sync.Mutex
		refSig    = make(chan struct{}, 1)
	)
	runner := loc.NewSessionRunner(loc.SessionRunnerOptions{
		Client: client,
		Handle: &loc.SessionHandle{
			SessionID:       sid,
			BrokerURL:       wsURL,
			Mode:            "session-control-plus-media@v0",
			PaymentEnvelope: "BASE64ENV",
		},
		OnRefillRefused: func(e loc.RefillEvent) {
			refMu.Lock()
			refused = e
			gotRef = true
			refMu.Unlock()
			select {
			case refSig <- struct{}{}:
			default:
			}
		},
	})
	if err := runner.Start(context.Background()); err != nil {
		t.Fatalf("start: %v", err)
	}
	select {
	case <-refSig:
	case <-time.After(2 * time.Second):
		t.Fatal("did not see refused event")
	}
	_, _ = runner.Close(context.Background(), loc.CloseSessionInput{ActualUnits: 0})

	refMu.Lock()
	defer refMu.Unlock()
	if !gotRef {
		t.Fatal("expected refused callback")
	}
	if refused.Error == nil {
		t.Error("refused event missing error")
	}
}

func TestSessionRunnerCloseIsIdempotent(t *testing.T) {
	sid := "55555555-5555-5555-5555-555555555555"
	closeCalls := 0
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/sessions/"+sid+"/close", func(w http.ResponseWriter, r *http.Request) {
		closeCalls++
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"session_id": sid, "outcome": "OVERFUNDED",
			"actual_units": float64(80), "billed_value_wei": float64(80000),
			"refund_wei": float64(120000), "closed_at": "2026-05-24T12:30:00Z",
		})
	})
	loca := httptest.NewServer(mux)
	defer loca.Close()

	wsURL, stop := wsServer(t, func(ctx context.Context, conn *websocket.Conn) {
		_, _, _ = conn.Read(ctx) // idle
	})
	defer stop()

	client, _ := loc.NewClient(loc.Options{BaseURL: loca.URL, APIKey: apiKey})
	runner := loc.NewSessionRunner(loc.SessionRunnerOptions{
		Client: client,
		Handle: &loc.SessionHandle{
			SessionID:       sid,
			BrokerURL:       wsURL,
			Mode:            "session-control-plus-media@v0",
			PaymentEnvelope: "BASE64ENV",
		},
	})
	if err := runner.Start(context.Background()); err != nil {
		t.Fatalf("start: %v", err)
	}
	if _, err := runner.Close(context.Background(), loc.CloseSessionInput{ActualUnits: 80}); err != nil {
		t.Fatalf("close: %v", err)
	}
	if _, err := runner.Close(context.Background(), loc.CloseSessionInput{ActualUnits: 80}); err != nil {
		t.Fatalf("second close: %v", err)
	}
	if closeCalls != 1 {
		t.Errorf("expected close called once, got %d", closeCalls)
	}
	if runner.Outcome() != "OVERFUNDED" {
		t.Errorf("outcome: %q", runner.Outcome())
	}
	if runner.BilledValueWei() != 80000 {
		t.Errorf("billed: %d", runner.BilledValueWei())
	}
	if runner.RefundWei() != 120000 {
		t.Errorf("refund: %d", runner.RefundWei())
	}
}
