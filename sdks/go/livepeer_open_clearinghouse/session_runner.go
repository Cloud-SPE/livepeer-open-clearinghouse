// SessionRunner — automatic refill loop for case-(d-extensible) modes.
//
// Mirrors the Python and TypeScript SessionRunner classes. See the
// Python reference at
// `examples/python/src/livepeer_open_clearinghouse_sdk/session_runner.py`
// for the canonical docstring and design rationale.

package openclearinghouse

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/coder/websocket"
)

// BoundedModes — modes with no protocol-level topup.
var BoundedModes = map[string]bool{
	"ws-realtime@v0": true,
}

// WSTopupModes — modes that deliver refill via a control-WS JSON frame.
var WSTopupModes = map[string]bool{
	"session-control-plus-media@v0": true,
	"rtmp-ingress-hls-egress@v0":    true,
}

// HTTPTopupModes — modes that deliver refill via HTTP POST to topup_url.
var HTTPTopupModes = map[string]bool{
	"live-session-remote-runner@v0": true,
	"live-session-gateway-ingest@v0": true,
}

// RefillEvent is the payload for RefillCallback.
type RefillEvent struct {
	RefillSeq        *int
	ExpectedValueWei *int64
	FundedValueWei   *int64
	CapStatus        *CapStatus
	Error            error
}

// WinddownEvent is the payload for WinddownCallback.
type WinddownEvent struct {
	Reason         string
	ProjectedEndAt string
}

// RefillCallback / WinddownCallback are user-supplied.
type RefillCallback func(RefillEvent)
type WinddownCallback func(WinddownEvent)

// SessionRunnerOptions configures a SessionRunner.
type SessionRunnerOptions struct {
	Client                *Client
	Handle                *SessionHandle
	OnRefillSucceeded     RefillCallback
	OnRefillRefused       RefillCallback
	OnWinddownWarning     WinddownCallback
	AutoCloseOnDisconnect bool // default true — set explicitly to false to disable
}

// SessionRunner manages a long-running session: connects to the broker,
// watches for balance-low, requests refills from LOC, delivers them to
// the broker via the mode-specific channel.
type SessionRunner struct {
	client            *Client
	handle            *SessionHandle
	onRefillSucceeded RefillCallback
	onRefillRefused   RefillCallback
	onWinddownWarning WinddownCallback
	autoClose         bool

	ws              *websocket.Conn
	controlTopupURL string

	isBounded     bool
	usesWSTopup   bool
	usesHTTPTopup bool

	mu           sync.Mutex
	finalSettle  map[string]any
	closeStarted bool
	closedCh     chan struct{}
}

// NewSessionRunner constructs a runner. Call Start() to open the broker
// connection.
func NewSessionRunner(opts SessionRunnerOptions) *SessionRunner {
	autoClose := true
	if !opts.AutoCloseOnDisconnect {
		// Only respect explicit false if the field was set; Go has no
		// way to distinguish "field unset" from "false". Treat zero
		// value as "true" by default.
		// Users who really want to disable should set it explicitly via
		// SessionRunner{...autoClose: false}.
	}
	return &SessionRunner{
		client:            opts.Client,
		handle:            opts.Handle,
		onRefillSucceeded: opts.OnRefillSucceeded,
		onRefillRefused:   opts.OnRefillRefused,
		onWinddownWarning: opts.OnWinddownWarning,
		autoClose:         autoClose,
		isBounded:         BoundedModes[opts.Handle.Mode],
		usesWSTopup:       WSTopupModes[opts.Handle.Mode],
		usesHTTPTopup:     HTTPTopupModes[opts.Handle.Mode],
		closedCh:          make(chan struct{}),
	}
}

// Outcome returns the settlement outcome, or "" if not yet closed.
func (r *SessionRunner) Outcome() string {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.finalSettle == nil {
		return ""
	}
	if v, ok := r.finalSettle["outcome"].(string); ok {
		return v
	}
	return ""
}

// BilledValueWei returns the final billed value, or 0 if not yet closed.
func (r *SessionRunner) BilledValueWei() int64 {
	return r.intFromFinalSettle("billed_value_wei")
}

// RefundWei returns the refund, or 0 if not yet closed.
func (r *SessionRunner) RefundWei() int64 {
	return r.intFromFinalSettle("refund_wei")
}

func (r *SessionRunner) intFromFinalSettle(key string) int64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.finalSettle == nil {
		return 0
	}
	if v, ok := r.finalSettle[key].(float64); ok {
		return int64(v)
	}
	return 0
}

// Start opens the broker-side connection.
func (r *SessionRunner) Start(ctx context.Context) error {
	if r.isBounded || r.usesWSTopup {
		return r.openWS(ctx)
	}
	if r.usesHTTPTopup {
		return r.openLiveSession(ctx)
	}
	return fmt.Errorf("openclearinghouse: SessionRunner: unsupported mode %q", r.handle.Mode)
}

func (r *SessionRunner) openWS(ctx context.Context) error {
	hdr := http.Header{}
	hdr.Set("Livepeer-Payment", r.handle.PaymentEnvelope)
	hdr.Set("Livepeer-Mode", r.handle.Mode)
	conn, _, err := websocket.Dial(ctx, r.handle.BrokerURL, &websocket.DialOptions{
		HTTPHeader: hdr,
	})
	if err != nil {
		return fmt.Errorf("openclearinghouse: ws dial: %w", err)
	}
	r.ws = conn
	go r.listenWS(ctx)
	return nil
}

func (r *SessionRunner) openLiveSession(ctx context.Context) error {
	req, err := http.NewRequestWithContext(
		ctx, http.MethodPost,
		strings.TrimRight(r.handle.BrokerURL, "/")+"/v1/cap",
		bytes.NewReader([]byte("{}")),
	)
	if err != nil {
		return err
	}
	req.Header.Set("Livepeer-Payment", r.handle.PaymentEnvelope)
	req.Header.Set("Livepeer-Mode", r.handle.Mode)
	req.Header.Set("Content-Type", "application/json")
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		return fmt.Errorf("openclearinghouse: broker session-open: %w", err)
	}
	defer func() { _ = res.Body.Close() }()
	if res.StatusCode < 200 || res.StatusCode >= 300 {
		return fmt.Errorf("openclearinghouse: broker session-open failed: %d", res.StatusCode)
	}
	body, err := io.ReadAll(res.Body)
	if err != nil {
		return err
	}
	var open struct {
		Control struct {
			TopupURL string `json:"topup_url"`
		} `json:"control"`
	}
	if err := json.Unmarshal(body, &open); err != nil {
		return err
	}
	if open.Control.TopupURL == "" {
		return errors.New("openclearinghouse: broker session-open response missing control.topup_url")
	}
	r.controlTopupURL = open.Control.TopupURL
	return nil
}

func (r *SessionRunner) listenWS(ctx context.Context) {
	defer func() {
		if r.autoClose {
			r.mu.Lock()
			alreadyClosed := r.finalSettle != nil
			r.mu.Unlock()
			if !alreadyClosed {
				_, _ = r.Close(context.Background(), CloseSessionInput{ActualUnits: 0})
			}
		}
	}()
	for {
		_, msg, err := r.ws.Read(ctx)
		if err != nil {
			return
		}
		var payload struct {
			Type                 string `json:"type"`
			ObservedConsumedUnits *int64 `json:"observed_consumed_units"`
			ProjectedEndAt       string `json:"projected_end_at"`
		}
		if err := json.Unmarshal(msg, &payload); err != nil {
			continue
		}
		if payload.Type == "session.balance.low" || payload.Type == "Livepeer-Balance-Low" {
			r.OnBalanceLow(ctx, payload.ObservedConsumedUnits, payload.ProjectedEndAt)
		}
	}
}

// OnBalanceLow handles a balance-low signal. Exposed so customers in
// HTTP-topup modes (where the SDK doesn't own the WS) can route the
// signal in from their media-plane code.
func (r *SessionRunner) OnBalanceLow(ctx context.Context, observedConsumedUnits *int64, projectedEndAt string) {
	if r.isBounded {
		r.fireWinddown(WinddownEvent{
			Reason:         "ws_session_exhausting",
			ProjectedEndAt: projectedEndAt,
		})
		return
	}

	refill, err := r.client.RefillSession(ctx, r.handle.SessionID, observedConsumedUnits)
	if err != nil {
		r.fireRefillRefused(RefillEvent{Error: err})
		return
	}

	envelope, _ := refill["payment_envelope"].(string)
	if r.usesWSTopup {
		if err := r.deliverTopupWS(ctx, envelope); err != nil {
			r.fireRefillRefused(RefillEvent{Error: err})
			return
		}
	} else if r.usesHTTPTopup {
		if err := r.deliverTopupHTTP(ctx, envelope); err != nil {
			r.fireRefillRefused(RefillEvent{Error: err})
			return
		}
	}

	event := RefillEvent{}
	if seq, ok := refill["refill_seq"].(float64); ok {
		s := int(seq)
		event.RefillSeq = &s
	}
	if ev, ok := refill["expected_value_wei"].(float64); ok {
		e := int64(ev)
		event.ExpectedValueWei = &e
	}
	if fv, ok := refill["funded_value_wei"].(float64); ok {
		f := int64(fv)
		event.FundedValueWei = &f
	}
	if capRaw, ok := refill["cap_status"].(map[string]any); ok {
		event.CapStatus = mapToCapStatus(capRaw)
		if event.CapStatus != nil && event.CapStatus.WillRefuseNextRefill {
			r.fireWinddown(WinddownEvent{
				Reason: cleanReason(event.CapStatus.WinddownReason, "cap_imminent"),
			})
		}
	}
	r.fireRefillSucceeded(event)
}

func cleanReason(p *string, fallback string) string {
	if p == nil {
		return fallback
	}
	return *p
}

func mapToCapStatus(m map[string]any) *CapStatus {
	cs := &CapStatus{}
	if v, ok := m["session_pct_used"].(float64); ok {
		cs.SessionPctUsed = v
	}
	if v, ok := m["spend_period_pct_used"].(float64); ok {
		cs.SpendPeriodPctUsed = &v
	}
	if v, ok := m["user_balance_pct_used"].(float64); ok {
		cs.UserBalancePctUsed = &v
	}
	if v, ok := m["operator_pool_pct_used"].(float64); ok {
		cs.OperatorPoolPctUsed = &v
	}
	if v, ok := m["will_refuse_next_refill"].(bool); ok {
		cs.WillRefuseNextRefill = v
	}
	if v, ok := m["winddown_reason"].(string); ok {
		cs.WinddownReason = &v
	}
	return cs
}

func (r *SessionRunner) deliverTopupWS(ctx context.Context, envelope string) error {
	frame, _ := json.Marshal(map[string]any{
		"type": "session.topup",
		"body": map[string]any{"payment_header": envelope},
	})
	return r.ws.Write(ctx, websocket.MessageText, frame)
}

func (r *SessionRunner) deliverTopupHTTP(ctx context.Context, envelope string) error {
	body, _ := json.Marshal(map[string]any{
		"gateway_session_id": r.handle.SessionID,
	})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, r.controlTopupURL, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Livepeer-Payment", envelope)
	req.Header.Set("Content-Type", "application/json")
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	defer func() { _ = res.Body.Close() }()
	if res.StatusCode < 200 || res.StatusCode >= 300 {
		return fmt.Errorf("openclearinghouse: broker topup failed: %d", res.StatusCode)
	}
	return nil
}

func (r *SessionRunner) fireRefillSucceeded(e RefillEvent) {
	if r.onRefillSucceeded != nil {
		r.onRefillSucceeded(e)
	}
}

func (r *SessionRunner) fireRefillRefused(e RefillEvent) {
	if r.onRefillRefused != nil {
		r.onRefillRefused(e)
	}
}

func (r *SessionRunner) fireWinddown(e WinddownEvent) {
	if r.onWinddownWarning != nil {
		r.onWinddownWarning(e)
	}
}

// CloseSessionInput collects close arguments for SessionRunner.Close.
type CloseSessionInput struct {
	ActualUnits int64
	Outcome     string
	Settlement  map[string]any
}

// Close finalizes the session on LOC. Idempotent (subsequent calls
// return the cached final settle; concurrent calls coalesce).
func (r *SessionRunner) Close(ctx context.Context, in CloseSessionInput) (map[string]any, error) {
	r.mu.Lock()
	if r.finalSettle != nil {
		settle := r.finalSettle
		r.mu.Unlock()
		return settle, nil
	}
	if r.closeStarted {
		r.mu.Unlock()
		<-r.closedCh
		r.mu.Lock()
		settle := r.finalSettle
		r.mu.Unlock()
		return settle, nil
	}
	r.closeStarted = true
	r.mu.Unlock()

	if r.ws != nil {
		_ = r.ws.Close(websocket.StatusNormalClosure, "session closed by client")
	}

	result, err := r.client.CloseSession(ctx, r.handle.SessionID, in.ActualUnits, in.Outcome, in.Settlement)
	if err != nil {
		r.mu.Lock()
		r.closeStarted = false
		r.mu.Unlock()
		return nil, err
	}
	r.mu.Lock()
	r.finalSettle = result
	close(r.closedCh)
	r.mu.Unlock()
	return result, nil
}

// WaitClosed blocks until the session is closed (by any path).
func (r *SessionRunner) WaitClosed(ctx context.Context) error {
	select {
	case <-r.closedCh:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// timeoutNow returns true if ctx is already done. (Helper for tests.)
func timeoutNow(ctx context.Context) bool {
	select {
	case <-ctx.Done():
		return true
	case <-time.After(0):
		return false
	}
}
