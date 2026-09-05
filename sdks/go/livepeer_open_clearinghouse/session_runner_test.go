package openclearinghouse_test

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"

	loc "github.com/livepeer/livepeer-open-clearinghouse-sdk-go/livepeer_open_clearinghouse"
)

func balance(status string, refuse bool) map[string]any {
	return map[string]any{
		"status": status, "claimed_units": 80, "debited_units": 80,
		"unit": "participant_minutes", "runway_units": 20,
		"runway_seconds_estimate": 1200, "will_refuse_next_refill": refuse,
	}
}

func sessionHandle(brokerURL, refill string) *loc.SessionHandle {
	sid := "11111111-1111-1111-1111-111111111111"
	return &loc.SessionHandle{
		SessionID: sid, RequestID: "open-request", WorkID: "wid", BrokerURL: brokerURL,
		Protocol: "paid-session/v1", Capability: "livepeer:test", Offering: "default",
		Session:       loc.SessionAxes{DescriptorSchema: "livepeer-session-test/v1", Attachment: "external", Metering: "runner-reported", Refill: refill},
		SessionParams: map[string]any{"room": "alpha"}, PaymentEnvelope: "OPEN-ENV",
		RefillEndpoint: "/v1/sessions/" + sid + "/refill", CloseEndpoint: "/v1/sessions/" + sid + "/close",
	}
}

func TestSessionRunnerPaidSessionV1HTTPControl(t *testing.T) {
	var brokerURL string
	var mu sync.Mutex
	seen := map[string]http.Header{}
	topupAttempts := 0
	broker := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		seen[r.URL.Path] = r.Header.Clone()
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/v1/session":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"session_id": "broker-session", "work_id": "wid", "state": "active",
				"runtime":    map[string]any{"schema": "livepeer-session-test/v1", "public": map[string]any{}, "grants": []any{}},
				"credential": "credential", "lease": map[string]any{"expires_at": "2026-08-21T00:00:00Z"},
				"balance": balance("ok", false),
				"control": map[string]any{"status_url": brokerURL + "/status", "topup_url": brokerURL + "/topup", "end_url": brokerURL + "/end"},
			})
		case "/topup":
			topupAttempts++
			if topupAttempts == 1 {
				w.WriteHeader(http.StatusServiceUnavailable)
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"balance": balance("ok", false)})
		case "/status":
			_ = json.NewEncoder(w).Encode(map[string]any{"state": "active", "balance": balance("ok", false)})
		case "/end":
			w.Header().Set("Livepeer-Settlement", encodedTestSettlement)
			w.WriteHeader(http.StatusNoContent)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	brokerURL = broker.URL
	defer broker.Close()

	sid := "11111111-1111-1111-1111-111111111111"
	refillCalls := 0
	locServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/v1/sessions/" + sid + "/refill":
			refillCalls++
			_ = json.NewEncoder(w).Encode(map[string]any{"request_id": "refill-request", "refill_seq": 1, "payment_envelope": "REFILL-ENV", "expected_value_wei": 50000, "funded_value_wei": 50000})
		case "/v1/sessions/" + sid + "/close":
			_ = json.NewEncoder(w).Encode(map[string]any{"outcome": "EXACT", "billed_value_wei": 150000, "refund_wei": 0})
		default:
			w.WriteHeader(http.StatusNoContent) // telemetry
		}
	}))
	defer locServer.Close()
	client, _ := loc.NewClient(loc.Options{BaseURL: locServer.URL, APIKey: apiKey})
	ctx := context.Background()
	first := loc.NewSessionRunner(loc.SessionRunnerOptions{Client: client, Handle: sessionHandle(brokerURL, "extensible")})
	if err := first.Start(ctx); err != nil {
		t.Fatal(err)
	}
	runner := loc.NewSessionRunner(loc.SessionRunnerOptions{Client: client, Handle: sessionHandle(brokerURL, "extensible")})
	if err := runner.Start(ctx); err != nil {
		t.Fatal(err)
	}
	status, err := runner.Status(ctx)
	if err != nil || status["state"] != "active" {
		t.Fatalf("status recovery failed: %v %v", status, err)
	}
	runner.OnBalance(ctx, loc.SessionBalance{Status: "low", ClaimedUnits: 80})
	runner.OnBalance(ctx, loc.SessionBalance{Status: "low", ClaimedUnits: 80})
	result, err := runner.Close(ctx, loc.CloseSessionInput{ActualUnits: 150})
	if err != nil {
		t.Fatal(err)
	}
	if result["outcome"] != "EXACT" {
		t.Fatalf("unexpected outcome: %v", result)
	}
	if seen["/v1/session"].Get("Livepeer-Protocol") != "paid-session/v1" {
		t.Fatal("missing protocol header")
	}
	if seen["/topup"].Get("Livepeer-Request-Id") != "refill-request" {
		t.Fatal("missing topup request id")
	}
	if seen["/topup"].Get("Authorization") != "Bearer credential" {
		t.Fatal("missing bearer credential")
	}
	if refillCalls != 1 || topupAttempts != 2 {
		t.Fatalf("retry duplicated credit: refill=%d topup=%d", refillCalls, topupAttempts)
	}
}

func TestSessionRunnerDrainsWithoutRefill(t *testing.T) {
	warnings := []string{}
	runner := loc.NewSessionRunner(loc.SessionRunnerOptions{
		Handle:            sessionHandle("http://unused", "bounded"),
		OnWinddownWarning: func(event loc.WinddownEvent) { warnings = append(warnings, event.Reason) },
	})
	runner.OnBalance(context.Background(), loc.SessionBalance{Status: "low"})
	runner.OnBalance(context.Background(), loc.SessionBalance{Status: "ok", WillRefuseNextRefill: true})
	if len(warnings) != 2 || warnings[0] != "bounded_runway_exhausting" || warnings[1] != "broker_will_refuse_next_refill" {
		t.Fatalf("unexpected warnings: %v", warnings)
	}
}

func TestSessionRunnerRebindsRecipientRotation(t *testing.T) {
	var brokerURL string
	var topupHeaders []http.Header
	var warnings []string
	topupCalls := 0
	broker := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Path == "/v1/session" {
			_ = json.NewEncoder(w).Encode(map[string]any{
				"session_id": "broker-session", "work_id": "wid", "state": "active",
				"runtime":    map[string]any{"schema": "livepeer-session-test/v1", "public": map[string]any{}},
				"credential": "credential", "lease": map[string]any{"expires_at": "2026-08-21T00:00:00Z"},
				"balance": balance("ok", false),
				"control": map[string]any{"status_url": brokerURL + "/status", "topup_url": brokerURL + "/topup", "end_url": brokerURL + "/end"},
			})
			return
		}
		if r.URL.Path == "/topup" {
			topupCalls++
			topupHeaders = append(topupHeaders, r.Header.Clone())
			if topupCalls == 1 {
				w.Header().Set("Livepeer-Error", "recipient_rotated")
				w.WriteHeader(http.StatusConflict)
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"balance": balance("ok", false)})
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	brokerURL = broker.URL
	defer broker.Close()

	sid := "11111111-1111-1111-1111-111111111111"
	var refillBodies []map[string]any
	var refillKeys []string
	locServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/sessions/"+sid+"/refill" {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		refillBodies = append(refillBodies, body)
		refillKeys = append(refillKeys, r.Header.Get("Idempotency-Key"))
		w.Header().Set("Content-Type", "application/json")
		if len(refillBodies) == 1 {
			_ = json.NewEncoder(w).Encode(map[string]any{"work_id": "wid", "request_id": "rejected", "payment_envelope": "OLD"})
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"work_id": "successor", "request_id": "replacement", "payment_envelope": "NEW", "rebind_from": "wid"})
	}))
	defer locServer.Close()
	client, _ := loc.NewClient(loc.Options{BaseURL: locServer.URL, APIKey: apiKey})
	runner := loc.NewSessionRunner(loc.SessionRunnerOptions{
		Client: client, Handle: sessionHandle(brokerURL, "extensible"),
		OnWinddownWarning: func(event loc.WinddownEvent) { warnings = append(warnings, event.Reason) },
	})
	ctx := context.Background()
	if err := runner.Start(ctx); err != nil {
		t.Fatal(err)
	}
	runner.OnBalance(ctx, loc.SessionBalance{Status: "low", ClaimedUnits: 80})

	if len(refillBodies) != 2 || refillBodies[1]["rebind_from"] != "wid" || refillBodies[1]["replaces_request_id"] != "rejected" {
		t.Fatalf("rotation binding missing: %v", refillBodies)
	}
	if refillKeys[0] == refillKeys[1] {
		t.Fatal("rotation reused LOC request identity")
	}
	if len(topupHeaders) != 2 || topupHeaders[1].Get("Livepeer-Rebind-From") != "wid" || topupHeaders[1].Get("Livepeer-Request-Id") != "replacement" {
		t.Fatalf("declared broker rebind missing: %v", topupHeaders)
	}
	if runner.BrokerSession().WorkID != "successor" {
		t.Fatalf("work id not advanced: %s", runner.BrokerSession().WorkID)
	}
	if len(warnings) != 0 {
		t.Fatalf("successful rotation became customer-visible: %v", warnings)
	}
}

func TestSessionRunnerDrainsWhenDeclaredRebindIsRefused(t *testing.T) {
	var brokerURL string
	topupCalls := 0
	broker := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Path == "/v1/session" {
			_ = json.NewEncoder(w).Encode(map[string]any{
				"session_id": "broker-session", "work_id": "wid", "state": "active",
				"runtime":    map[string]any{"schema": "livepeer-session-test/v1", "public": map[string]any{}},
				"credential": "credential", "lease": map[string]any{"expires_at": "2026-08-21T00:00:00Z"},
				"balance": balance("ok", false),
				"control": map[string]any{"status_url": brokerURL + "/status", "topup_url": brokerURL + "/topup", "end_url": brokerURL + "/end"},
			})
			return
		}
		if r.URL.Path == "/topup" {
			topupCalls++
			w.Header().Set("Livepeer-Error", map[bool]string{true: "recipient_rotated", false: "rebind_refused"}[topupCalls == 1])
			w.WriteHeader(http.StatusConflict)
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	brokerURL = broker.URL
	defer broker.Close()

	sid := "11111111-1111-1111-1111-111111111111"
	refillCalls := 0
	locServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/sessions/"+sid+"/refill" {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		refillCalls++
		w.Header().Set("Content-Type", "application/json")
		if refillCalls == 1 {
			_ = json.NewEncoder(w).Encode(map[string]any{"work_id": "wid", "request_id": "rejected", "payment_envelope": "OLD"})
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"work_id": "successor", "request_id": "replacement", "payment_envelope": "NEW", "rebind_from": "wid"})
	}))
	defer locServer.Close()
	warnings := []string{}
	client, _ := loc.NewClient(loc.Options{BaseURL: locServer.URL, APIKey: apiKey})
	runner := loc.NewSessionRunner(loc.SessionRunnerOptions{
		Client: client, Handle: sessionHandle(brokerURL, "extensible"),
		OnWinddownWarning: func(event loc.WinddownEvent) { warnings = append(warnings, event.Reason) },
	})
	if err := runner.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	runner.OnBalance(context.Background(), loc.SessionBalance{Status: "low", ClaimedUnits: 80})
	if refillCalls != 2 || topupCalls != 2 {
		t.Fatalf("unexpected retries: refill=%d topup=%d", refillCalls, topupCalls)
	}
	if len(warnings) != 1 || warnings[0] != "payment_unrecoverable" {
		t.Fatalf("unexpected drain signal: %v", warnings)
	}
}

func TestSessionRunnerRejectsDescriptorMismatch(t *testing.T) {
	broker := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"session_id": "broker-session", "work_id": "wid", "state": "active",
			"runtime":    map[string]any{"schema": "wrong/v1", "public": map[string]any{}},
			"credential": "credential", "lease": map[string]any{"expires_at": "2026-08-21T00:00:00Z"},
			"balance": balance("ok", false),
			"control": map[string]any{"status_url": "x", "topup_url": "x", "end_url": "x"},
		})
	}))
	defer broker.Close()
	runner := loc.NewSessionRunner(loc.SessionRunnerOptions{Handle: sessionHandle(broker.URL, "extensible")})
	if err := runner.Start(context.Background()); err == nil {
		t.Fatal("expected descriptor mismatch")
	}
}
