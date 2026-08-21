// Package openclearinghouse provides the paid-session/v1 control driver.
package openclearinghouse

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"sync"

	"github.com/coder/websocket"
)

type SessionBalance struct {
	Status                string `json:"status"`
	ClaimedUnits          int64  `json:"claimed_units"`
	DebitedUnits          int64  `json:"debited_units"`
	Unit                  string `json:"unit"`
	RunwayUnits           int64  `json:"runway_units"`
	RunwaySecondsEstimate *int64 `json:"runway_seconds_estimate"`
	WillRefuseNextRefill  bool   `json:"will_refuse_next_refill"`
}

type BrokerControl struct {
	StatusURL string `json:"status_url"`
	TopupURL  string `json:"topup_url"`
	EndURL    string `json:"end_url"`
	EventsWS  string `json:"events_ws"`
}

type BrokerRuntime struct {
	Schema string           `json:"schema"`
	Public map[string]any   `json:"public"`
	Grants []map[string]any `json:"grants"`
}

type BrokerSession struct {
	SessionID  string        `json:"session_id"`
	WorkID     string        `json:"work_id"`
	State      string        `json:"state"`
	Runtime    BrokerRuntime `json:"runtime"`
	Credential string        `json:"credential"`
	Lease      struct {
		ExpiresAt string `json:"expires_at"`
	} `json:"lease"`
	Balance SessionBalance `json:"balance"`
	Control BrokerControl  `json:"control"`
}

type RefillEvent struct {
	RefillSeq        *int
	ExpectedValueWei *int64
	FundedValueWei   *int64
	CapStatus        *CapStatus
	Error            error
}

type WinddownEvent struct {
	Reason         string
	ProjectedEndAt string
}

type RefillCallback func(RefillEvent)
type WinddownCallback func(WinddownEvent)

type SessionRunnerOptions struct {
	Client            *Client
	Handle            *SessionHandle
	HTTP              *http.Client
	OnRefillSucceeded RefillCallback
	OnRefillRefused   RefillCallback
	OnWinddownWarning WinddownCallback
}

type SessionRunner struct {
	client            *Client
	handle            *SessionHandle
	http              *http.Client
	onRefillSucceeded RefillCallback
	onRefillRefused   RefillCallback
	onWinddownWarning WinddownCallback
	ws                *websocket.Conn
	broker            *BrokerSession
	pendingKey        string
	pendingRefill     map[string]any
	mu                sync.Mutex
	finalSettle       map[string]any
	closeStarted      bool
	closedCh          chan struct{}
}

func NewSessionRunner(opts SessionRunnerOptions) *SessionRunner {
	httpClient := opts.HTTP
	if httpClient == nil {
		httpClient = http.DefaultClient
	}
	return &SessionRunner{
		client: opts.Client, handle: opts.Handle, http: httpClient,
		onRefillSucceeded: opts.OnRefillSucceeded,
		onRefillRefused:   opts.OnRefillRefused,
		onWinddownWarning: opts.OnWinddownWarning,
		closedCh:          make(chan struct{}),
	}
}

func (r *SessionRunner) Start(ctx context.Context) error {
	r.mu.Lock()
	if r.broker != nil {
		r.mu.Unlock()
		return nil
	}
	r.mu.Unlock()
	body, _ := json.Marshal(map[string]any{
		"gateway_session_id": r.handle.SessionID,
		"session_params":     r.handle.SessionParams,
	})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		strings.TrimRight(r.handle.BrokerURL, "/")+"/v1/session", bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Livepeer-Protocol", r.handle.Protocol)
	req.Header.Set("Livepeer-Capability", r.handle.Capability)
	req.Header.Set("Livepeer-Offering", r.handle.Offering)
	req.Header.Set("Livepeer-Request-Id", r.handle.RequestID)
	req.Header.Set("Livepeer-Payment", r.handle.PaymentEnvelope)
	res, err := r.http.Do(req)
	if err != nil {
		return fmt.Errorf("openclearinghouse: broker session-open: %w", err)
	}
	defer func() { _ = res.Body.Close() }()
	if res.StatusCode < 200 || res.StatusCode >= 300 {
		return fmt.Errorf("openclearinghouse: broker session-open failed: %d", res.StatusCode)
	}
	var session BrokerSession
	if err := json.NewDecoder(res.Body).Decode(&session); err != nil {
		return err
	}
	if err := validateBrokerSession(&session, r.handle); err != nil {
		return err
	}
	r.mu.Lock()
	r.broker = &session
	r.mu.Unlock()
	if session.Control.EventsWS != "" {
		hdr := http.Header{"Authorization": []string{"Bearer " + session.Credential}}
		conn, _, err := websocket.Dial(ctx, session.Control.EventsWS, &websocket.DialOptions{HTTPHeader: hdr})
		if err != nil {
			return fmt.Errorf("openclearinghouse: events ws: %w", err)
		}
		r.ws = conn
		go r.listen(ctx)
	}
	return nil
}

func validateBrokerSession(session *BrokerSession, handle *SessionHandle) error {
	if session.WorkID != handle.WorkID {
		return fmt.Errorf("openclearinghouse: broker work_id mismatch")
	}
	if session.Runtime.Schema != handle.Session.DescriptorSchema {
		return fmt.Errorf("openclearinghouse: descriptor schema mismatch")
	}
	if session.SessionID == "" || session.Credential == "" || session.Control.StatusURL == "" ||
		session.Control.TopupURL == "" || session.Control.EndURL == "" {
		return fmt.Errorf("openclearinghouse: malformed broker session-open response")
	}
	return validateBalance(session.Balance)
}

func validateBalance(balance SessionBalance) error {
	if balance.Status != "ok" && balance.Status != "low" && balance.Status != "exhausted" {
		return fmt.Errorf("openclearinghouse: invalid balance status %q", balance.Status)
	}
	return nil
}

func (r *SessionRunner) BrokerSession() *BrokerSession {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.broker
}

func (r *SessionRunner) Status(ctx context.Context) (map[string]any, error) {
	if err := r.Start(ctx); err != nil {
		return nil, err
	}
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, r.broker.Control.StatusURL, nil)
	req.Header.Set("Authorization", "Bearer "+r.broker.Credential)
	res, err := r.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer func() { _ = res.Body.Close() }()
	if res.StatusCode < 200 || res.StatusCode >= 300 {
		return nil, fmt.Errorf("broker status: %d", res.StatusCode)
	}
	var out map[string]any
	return out, json.NewDecoder(res.Body).Decode(&out)
}

func (r *SessionRunner) listen(ctx context.Context) {
	for {
		_, raw, err := r.ws.Read(ctx)
		if err != nil {
			return
		}
		var event struct {
			Type    string         `json:"type"`
			Balance SessionBalance `json:"balance"`
		}
		if json.Unmarshal(raw, &event) == nil && event.Type == "session.balance" {
			r.OnBalance(ctx, event.Balance)
		}
	}
}

func (r *SessionRunner) OnBalance(ctx context.Context, balance SessionBalance) {
	if validateBalance(balance) != nil {
		return
	}
	if balance.WillRefuseNextRefill {
		r.fireWinddown(WinddownEvent{Reason: "broker_will_refuse_next_refill"})
		return
	}
	if balance.Status != "low" {
		return
	}
	if r.handle.Session.Refill == "bounded" {
		r.fireWinddown(WinddownEvent{Reason: "bounded_runway_exhausting"})
		return
	}
	if err := r.refill(ctx, balance.ClaimedUnits); err != nil {
		r.fireRefillRefused(RefillEvent{Error: err})
	}
}

func (r *SessionRunner) refill(ctx context.Context, observed int64) error {
	if err := r.Start(ctx); err != nil {
		return err
	}
	r.mu.Lock()
	if r.pendingKey == "" {
		r.pendingKey = newUUIDv4()
	}
	key, refill := r.pendingKey, r.pendingRefill
	r.mu.Unlock()
	if refill == nil {
		var err error
		refill, err = r.client.RefillSession(ctx, r.handle.SessionID, &observed, key, "", "")
		if err != nil {
			return err
		}
		r.mu.Lock()
		r.pendingRefill = refill
		r.mu.Unlock()
	}
	res, err := r.postTopup(ctx, refill)
	if err != nil {
		return err
	}
	if brokerError(res) == "recipient_rotated" {
		_ = res.Body.Close()
		if refill["rebind_from"] != nil {
			r.endUnrecoverableRotation()
			return nil
		}
		predecessor := fmt.Sprint(refill["work_id"])
		replacementKey := newUUIDv4()
		r.mu.Lock()
		r.pendingKey = replacementKey
		r.mu.Unlock()
		refill, err = r.client.RefillSession(ctx, r.handle.SessionID, &observed,
			replacementKey, predecessor, fmt.Sprint(refill["request_id"]))
		if err != nil {
			return err
		}
		r.mu.Lock()
		r.pendingRefill = refill
		r.mu.Unlock()
		res, err = r.postTopup(ctx, refill)
		if err != nil {
			return err
		}
	}
	defer func() { _ = res.Body.Close() }()
	if brokerError(res) == "recipient_rotated" {
		r.endUnrecoverableRotation()
		return nil
	}
	if brokerError(res) == "rebind_refused" {
		r.endUnrecoverableRotation()
		return nil
	}
	if res.StatusCode < 200 || res.StatusCode >= 300 {
		return fmt.Errorf("broker topup: %d", res.StatusCode)
	}
	if refill["rebind_from"] != nil {
		r.mu.Lock()
		r.broker.WorkID = fmt.Sprint(refill["work_id"])
		r.mu.Unlock()
	}
	event := refillEvent(refill)
	r.fireRefillSucceeded(event)
	r.mu.Lock()
	r.pendingKey = ""
	r.pendingRefill = nil
	r.mu.Unlock()
	return nil
}

func (r *SessionRunner) postTopup(ctx context.Context, refill map[string]any) (*http.Response, error) {
	body := bytes.NewReader([]byte("{}"))
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, r.broker.Control.TopupURL, body)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+r.broker.Credential)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Livepeer-Payment", fmt.Sprint(refill["payment_envelope"]))
	req.Header.Set("Livepeer-Request-Id", fmt.Sprint(refill["request_id"]))
	if rebindFrom := refill["rebind_from"]; rebindFrom != nil {
		req.Header.Set("Livepeer-Rebind-From", fmt.Sprint(rebindFrom))
	}
	return r.http.Do(req)
}

func brokerError(response *http.Response) string {
	if response.StatusCode != http.StatusConflict {
		return ""
	}
	return response.Header.Get("Livepeer-Error")
}

func (r *SessionRunner) endUnrecoverableRotation() {
	r.mu.Lock()
	r.pendingKey = ""
	r.pendingRefill = nil
	r.mu.Unlock()
	r.fireWinddown(WinddownEvent{Reason: "payment_unrecoverable"})
}

func refillEvent(refill map[string]any) RefillEvent {
	event := RefillEvent{}
	if n, ok := refill["refill_seq"].(float64); ok {
		value := int(n)
		event.RefillSeq = &value
	}
	if n, ok := refill["expected_value_wei"].(float64); ok {
		value := int64(n)
		event.ExpectedValueWei = &value
	}
	if n, ok := refill["funded_value_wei"].(float64); ok {
		value := int64(n)
		event.FundedValueWei = &value
	}
	if raw, ok := refill["cap_status"].(map[string]any); ok {
		event.CapStatus = mapToCapStatus(raw)
	}
	return event
}

func mapToCapStatus(raw map[string]any) *CapStatus {
	encoded, _ := json.Marshal(raw)
	var status CapStatus
	if json.Unmarshal(encoded, &status) != nil {
		return nil
	}
	return &status
}

func (r *SessionRunner) fireRefillSucceeded(event RefillEvent) {
	if r.onRefillSucceeded != nil {
		r.onRefillSucceeded(event)
	}
}
func (r *SessionRunner) fireRefillRefused(event RefillEvent) {
	if r.onRefillRefused != nil {
		r.onRefillRefused(event)
	}
}
func (r *SessionRunner) fireWinddown(event WinddownEvent) {
	if r.onWinddownWarning != nil {
		r.onWinddownWarning(event)
	}
}

type CloseSessionInput struct {
	ActualUnits int64
	Outcome     string
}

func (r *SessionRunner) Close(ctx context.Context, in CloseSessionInput) (map[string]any, error) {
	r.mu.Lock()
	if r.finalSettle != nil {
		result := r.finalSettle
		r.mu.Unlock()
		return result, nil
	}
	if r.closeStarted {
		r.mu.Unlock()
		select {
		case <-r.closedCh:
			return r.finalSettle, nil
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	r.closeStarted = true
	r.mu.Unlock()
	if err := r.Start(ctx); err != nil {
		return nil, err
	}
	body, _ := json.Marshal(map[string]string{"reason": "gateway_close"})
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, r.broker.Control.EndURL, bytes.NewReader(body))
	req.Header.Set("Authorization", "Bearer "+r.broker.Credential)
	req.Header.Set("Content-Type", "application/json")
	res, err := r.http.Do(req)
	if err != nil {
		return nil, err
	}
	_ = res.Body.Close()
	if res.StatusCode < 200 || res.StatusCode >= 300 {
		return nil, fmt.Errorf("broker end: %d", res.StatusCode)
	}
	encodedSettlement := res.Header.Get("Livepeer-Settlement")
	if encodedSettlement == "" {
		return nil, &BrokerProtocolError{Code: "broker_protocol_error", Message: "broker end response missing Livepeer-Settlement", Status: res.StatusCode}
	}
	rawSettlement, err := base64.StdEncoding.DecodeString(encodedSettlement)
	if err != nil {
		return nil, &BrokerProtocolError{Code: "broker_protocol_error", Message: "broker end response has malformed Livepeer-Settlement", Status: res.StatusCode}
	}
	var settlement map[string]any
	if err := json.Unmarshal(rawSettlement, &settlement); err != nil || settlement == nil {
		return nil, &BrokerProtocolError{Code: "broker_protocol_error", Message: "broker end response has malformed Livepeer-Settlement", Status: res.StatusCode}
	}
	if r.ws != nil {
		_ = r.ws.Close(websocket.StatusNormalClosure, "gateway close")
	}
	result, err := r.client.CloseSession(ctx, r.handle.SessionID, in.ActualUnits, in.Outcome, settlement)
	if err != nil {
		return nil, err
	}
	r.mu.Lock()
	r.finalSettle = result
	close(r.closedCh)
	r.mu.Unlock()
	return result, nil
}

func (r *SessionRunner) Outcome() string {
	r.mu.Lock()
	defer r.mu.Unlock()
	value, _ := r.finalSettle["outcome"].(string)
	return value
}
func (r *SessionRunner) BilledValueWei() int64 { return r.finalInt("billed_value_wei") }
func (r *SessionRunner) RefundWei() int64      { return r.finalInt("refund_wei") }
func (r *SessionRunner) finalInt(key string) int64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	value, _ := r.finalSettle[key].(float64)
	return int64(value)
}

func (r *SessionRunner) WaitClosed(ctx context.Context) error {
	select {
	case <-r.closedCh:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}
