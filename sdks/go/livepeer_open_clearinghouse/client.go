// Package openclearinghouse is a reference Go SDK for the Livepeer
// Open Clearinghouse payment clearinghouse in handoff mode (exec-plan
// 002). It wraps the few HTTP endpoints app developers need: discovery,
// jobs (cases a/b/c) and sessions (case d).
//
// Construct one Client per process and reuse it. The zero value is not
// useful — always go through NewClient.
//
// # Wire-shape source of truth
//
// Response types below (JobOpenResponse, etc.) are mirrored by the
// oapi-codegen output in _generated.go, which is regenerated from the
// gateway's /openapi.json. The hand-typed versions exist for
// ergonomics; _generated.go is a drift-detection target.
//
// Regen recipe (from repo root):
//
//	make refresh-openapi
//
// then from this directory:
//
//	oapi-codegen -config /tmp/oapi-codegen.yaml /tmp/openapi-3.0.json \
//	    > _generated.go
package openclearinghouse

import (
	"bytes"
	"context"
	cryptorand "crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"
)

// runtimeGoVersion is split out so tests can swap it.
var runtimeGoVersion = func() string {
	return strings.TrimPrefix(runtime.Version(), "go")
}

// errorCode unwraps a typed *Error to its `code` for telemetry; non-Error
// returns the empty string.
func errorCode(err error) string {
	var e *Error
	if errors.As(err, &e) {
		return e.Code
	}
	return ""
}

// SDK identity sent on every request to LOC for operator-side trust
// scoring. Operators reject obviously-stale versions per the design.
const (
	SDKLang    = "go"
	SDKVersion = "1.3.3"
	SDKGitSHA  = "dev" // overwritten at build time
)

// SDKIdentity is the value sent in the Livepeer-Open-Clearinghouse-SDK
// header on every LOC request.
var SDKIdentity = fmt.Sprintf("%s/%s/%s", SDKLang, SDKVersion, SDKGitSHA)

// CapStatus is the cap-headroom snapshot returned with refill and settle
// responses. Percentages are in [0, 1]; nil means the cap isn't enabled.
type CapStatus struct {
	SessionPctUsed       float64  `json:"session_pct_used"`
	SpendPeriodPctUsed   *float64 `json:"spend_period_pct_used"`
	UserBalancePctUsed   *float64 `json:"user_balance_pct_used"`
	OperatorPoolPctUsed  *float64 `json:"operator_pool_pct_used"`
	WillRefuseNextRefill bool     `json:"will_refuse_next_refill"`
	WinddownReason       *string  `json:"winddown_reason"`
}

// Capability mirrors the registry's per-capability shape.
type Capability struct {
	Name      string     `json:"name"`
	WorkUnit  string     `json:"work_unit"`
	Offerings []Offering `json:"offerings"`
}

// Offering is one priced tier under a capability.
type Offering struct {
	ID                  string `json:"id"`
	PricePerWorkUnitWei string `json:"price_per_work_unit_wei"`
	WorkUnit            string `json:"work_unit"`
}

// Orchestrator is one orch endpoint with its capability set.
type Orchestrator struct {
	EthAddress      string       `json:"eth_address"`
	WorkerURL       string       `json:"worker_url"`
	Capabilities    []Capability `json:"capabilities"`
	SignatureStatus string       `json:"signature_status"`
	FreshnessStatus string       `json:"freshness_status"`
}

// JobOpenResponse mirrors POST /v1/jobs response.
type JobOpenResponse struct {
	JobID            string `json:"job_id"`
	RequestID        string `json:"request_id"`
	WorkID           string `json:"work_id"`
	BrokerURL        string `json:"broker_url"`
	Protocol         string `json:"protocol"`
	Transport        string `json:"transport"`
	WorkUnit         string `json:"work_unit"`
	PaymentEnvelope  string `json:"payment_envelope"`
	ExpectedValueWei int64  `json:"expected_value_wei"`
	FundedValueWei   int64  `json:"funded_value_wei"`
	SettleEndpoint   string `json:"settle_endpoint"`
	OpenedAt         string `json:"opened_at"`
}

// JobSettleResponse mirrors POST /v1/jobs/{id}/settle response.
type JobSettleResponse struct {
	JobID          string    `json:"job_id"`
	WorkID         string    `json:"work_id"`
	ActualUnits    int64     `json:"actual_units"`
	BilledValueWei int64     `json:"billed_value_wei"`
	RefundWei      int64     `json:"refund_wei"`
	Outcome        string    `json:"outcome"`
	ClosedAt       string    `json:"closed_at"`
	CapStatus      CapStatus `json:"cap_status"`
}

// JobResult is the end-to-end return of SubmitJob — the broker's
// response wrapped with the LOC-side settlement record.
type JobResult struct {
	// Body is the broker's response body. JSON when the Content-Type
	// indicates it, otherwise the raw bytes are in BodyText.
	Body           json.RawMessage
	BodyText       string
	Status         int
	JobID          string
	WorkID         string
	BrokerJobID    string
	Protocol       string
	Transport      string
	WorkUnit       string
	ActualUnits    int64
	BilledValueWei int64
	RefundWei      int64
	Outcome        string
	CapStatus      CapStatus
	RequestID      string
	RawHeaders     http.Header
}

// SessionHandle is the outbound from OpenSession (case d). Carries the
// broker URL + minted envelope; the caller drives the broker WS/RTMP
// wire today.
type SessionHandle struct {
	SessionID        string `json:"session_id"`
	WorkID           string `json:"work_id"`
	BrokerURL        string `json:"broker_url"`
	Mode             string `json:"mode"`
	PaymentEnvelope  string `json:"payment_envelope"`
	ExpectedValueWei int64  `json:"expected_value_wei"`
	FundedValueWei   int64  `json:"funded_value_wei"`
	RefillEndpoint   string `json:"refill_endpoint"`
	CloseEndpoint    string `json:"close_endpoint"`
	OpenedAt         string `json:"opened_at"`
}

// Client is the async HTTP client.
type Client struct {
	baseURL     string
	apiKey      string
	sdkIdentity string
	http        *http.Client
	telemetry   *TelemetryEmitter

	initOnce sync.Once
}

// Options is the input to NewClient.
type Options struct {
	BaseURL string
	APIKey  string
	// Optional. Pass an *http.Client with your own timeouts/transport.
	// Defaults to one with a 15s timeout.
	HTTP *http.Client
	// Optional override for the SDK identity header value.
	SDKIdentity string
}

// NewClient validates inputs and returns a ready-to-use Client.
func NewClient(opts Options) (*Client, error) {
	if !strings.HasPrefix(opts.APIKey, "pymth_") {
		return nil, errors.New("apiKey looks wrong (expected to start with pymth_)")
	}
	httpClient := opts.HTTP
	if httpClient == nil {
		httpClient = &http.Client{Timeout: 15 * time.Second}
	}
	sdkID := opts.SDKIdentity
	if sdkID == "" {
		sdkID = SDKIdentity
	}
	c := &Client{
		baseURL:     strings.TrimRight(opts.BaseURL, "/"),
		apiKey:      opts.APIKey,
		sdkIdentity: sdkID,
		http:        httpClient,
	}
	c.telemetry = newTelemetryEmitter(TelemetryEmitterOptions{
		HTTP:        httpClient,
		BaseURL:     c.baseURL,
		APIKey:      opts.APIKey,
		SDKIdentity: sdkID,
	})
	return c, nil
}

// Telemetry exposes the SDK's telemetry emitter for advanced cases.
// Most users never touch this — events fire automatically from the
// load-bearing Client methods.
func (c *Client) Telemetry() *TelemetryEmitter {
	return c.telemetry
}

// Close drains the telemetry buffer with one final flush. Idempotent.
func (c *Client) Close(ctx context.Context) {
	c.telemetry.Close(ctx)
}

// emitSdkInitOnce emits the `sdk.init` event the first time any
// telemetry-producing method runs.
func (c *Client) emitSdkInitOnce() {
	c.initOnce.Do(func() {
		c.telemetry.Emit(EmitTelemetryOptions{
			EventType: "sdk.init",
			Payload: map[string]interface{}{
				"lang":            SDKLang,
				"semver":          SDKVersion,
				"git_sha7":        SDKGitSHA,
				"runtime_version": "go/" + runtimeGoVersion(),
			},
		})
	})
}

// ---- discovery ----

// ListCapabilities returns the capability catalog the gateway is currently
// advertising via service-registry-daemon.
func (c *Client) ListCapabilities(ctx context.Context) ([]Capability, error) {
	var resp struct {
		Items []Capability `json:"items"`
	}
	if err := c.do(ctx, http.MethodGet, "/v1/capabilities", nil, &resp); err != nil {
		return nil, err
	}
	return resp.Items, nil
}

// ListOrchestrators returns the orchestrator catalog. Pass capability=""
// for the full list, or a capability name to filter.
func (c *Client) ListOrchestrators(ctx context.Context, capability string) ([]Orchestrator, error) {
	path := "/v1/orchestrators"
	if capability != "" {
		path += "?capability=" + url.QueryEscape(capability)
	}
	var resp struct {
		Items []Orchestrator `json:"items"`
	}
	if err := c.do(ctx, http.MethodGet, path, nil, &resp); err != nil {
		return nil, err
	}
	return resp.Items, nil
}

// ---- jobs (cases a/b/c) ----

// SubmitJobInput collects the arguments for SubmitJob.
type SubmitJobInput struct {
	Capability     string
	Offering       string
	EstimatedUnits int64
	Body           []byte // raw bytes; caller marshals JSON if needed
	ContentType    string // defaults to application/json if Body starts with {/[, else octet-stream
	MaxTotalUnits  int64  // optional; defaults to EstimatedUnits
	RequestID      string // optional; SubmitJob generates a UUID if empty
	Transport      string // unary (default), stream, or multipart
	Timeout        time.Duration
}

func normalizedTransport(transport string) string {
	if transport == "" {
		return "unary"
	}
	return transport
}

// SubmitJob is the load-bearing convenience method: opens a job via
// POST /v1/jobs (which mints a payment envelope), calls the broker
// with that envelope as Livepeer-Payment, reads Livepeer-Work-Units
// from the broker's response, then settles via POST /v1/jobs/{id}/settle.
//
// Returns the broker's response body + status alongside the LOC-side
// settlement (billed, refund, cap_status). Broker-level non-2xx is
// returned in the result, not raised; only LOC-side errors become
// non-nil err.
func (c *Client) SubmitJob(ctx context.Context, in SubmitJobInput) (*JobResult, error) {
	// 1. Open the job
	c.emitSdkInitOnce()
	requestID := in.RequestID
	if requestID == "" {
		requestID = newUUIDv4()
	}
	transport := normalizedTransport(in.Transport)
	if transport != "unary" && transport != "stream" && transport != "multipart" {
		return nil, &BrokerProtocolError{Code: "protocol_transport_unsupported", Message: fmt.Sprintf("unsupported transport %q", transport)}
	}
	if transport == "multipart" && !strings.HasPrefix(strings.ToLower(in.ContentType), "multipart/form-data") {
		return nil, &BrokerProtocolError{Code: "protocol_transport_mismatch", Message: "multipart transport requires multipart/form-data Content-Type"}
	}
	c.telemetry.Emit(EmitTelemetryOptions{
		EventType:     "request.mint_started",
		CorrelationID: requestID,
		Payload: map[string]interface{}{
			"capability":      in.Capability,
			"offering":        in.Offering,
			"estimated_units": in.EstimatedUnits,
		},
	})
	mintStarted := time.Now()

	body := map[string]any{
		"capability":      in.Capability,
		"offering":        in.Offering,
		"transport":       transport,
		"estimated_units": in.EstimatedUnits,
	}
	if in.MaxTotalUnits > 0 {
		body["max_total_units"] = in.MaxTotalUnits
	} else {
		body["max_total_units"] = nil
	}
	var job JobOpenResponse
	if err := c.doWithHeaders(ctx, http.MethodPost, "/v1/jobs", body, &job, http.Header{
		"Idempotency-Key": []string{requestID},
	}); err != nil {
		c.telemetry.Emit(EmitTelemetryOptions{
			EventType:     "request.error",
			CorrelationID: requestID,
			Payload: map[string]interface{}{
				"phase":       "mint",
				"error_class": fmt.Sprintf("%T", err),
				"error_code":  errorCode(err),
			},
		})
		return nil, err
	}
	c.telemetry.Emit(EmitTelemetryOptions{
		EventType:     "request.mint_completed",
		CorrelationID: requestID,
		Payload: map[string]interface{}{
			"latency_ms":       time.Since(mintStarted).Milliseconds(),
			"funded_value_wei": job.FundedValueWei,
			"protocol":         job.Protocol,
		},
	})
	if job.Protocol != "paid-job/v1" {
		return nil, &BrokerProtocolError{Code: "protocol_unsupported", Message: fmt.Sprintf("LOC returned protocol %q", job.Protocol)}
	}
	if job.Transport != transport {
		return nil, &BrokerProtocolError{Code: "protocol_transport_mismatch", Message: fmt.Sprintf("LOC returned transport %q; requested %q", job.Transport, transport)}
	}

	// 2. Call the broker directly
	contentType := in.ContentType
	if contentType == "" {
		if len(in.Body) > 0 && (in.Body[0] == '{' || in.Body[0] == '[') {
			contentType = "application/json"
		} else {
			contentType = "application/octet-stream"
		}
	}
	timeout := in.Timeout
	if timeout == 0 {
		timeout = 60 * time.Second
	}
	endpoint := strings.TrimRight(job.BrokerURL, "/") + "/v1/job"

	brokerCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	req, err := http.NewRequestWithContext(brokerCtx, http.MethodPost, endpoint, bytes.NewReader(in.Body))
	if err != nil {
		return nil, fmt.Errorf("openclearinghouse: build broker request: %w", err)
	}
	req.Header.Set("Livepeer-Capability", in.Capability)
	req.Header.Set("Livepeer-Offering", in.Offering)
	req.Header.Set("Livepeer-Payment", job.PaymentEnvelope)
	req.Header.Set("Livepeer-Protocol", job.Protocol)
	req.Header.Set("Livepeer-Request-Id", job.RequestID)
	req.Header.Set("Content-Type", contentType)
	if transport == "stream" {
		req.Header.Set("Accept", "text/event-stream")
	}

	status, header, payload, brokerErr := readBroker(c.http, req)
	if brokerErr != nil {
		return nil, brokerErr
	}

	// 3. Read Livepeer-Work-Units from the broker response
	workUnits := header.Get("Livepeer-Work-Units")
	brokerWorkUnit := header.Get("Livepeer-Work-Unit")
	brokerJobID := header.Get("Livepeer-Job-Id")
	if workUnits == "" || brokerWorkUnit == "" || brokerJobID == "" {
		return nil, &BrokerProtocolError{Code: "broker_protocol_error", Message: "terminal response missing Work-Units, Work-Unit, or Job-Id", Status: status}
	}
	actualUnits, parseErr := strconv.ParseInt(workUnits, 10, 64)
	if parseErr != nil || actualUnits < 0 {
		return nil, &BrokerProtocolError{Code: "broker_protocol_error", Message: "invalid Livepeer-Work-Units", Status: status}
	}
	if brokerWorkUnit != job.WorkUnit {
		return nil, &BrokerProtocolError{Code: "work_unit_mismatch", Message: fmt.Sprintf("broker reported work unit %q; expected %q", brokerWorkUnit, job.WorkUnit), Status: status}
	}

	// 4. Settle. Best-effort for caller compatibility; telemetry records failure.
	c.telemetry.Emit(EmitTelemetryOptions{
		EventType:     "request.settle_started",
		CorrelationID: requestID,
	})
	settleStarted := time.Now()
	settleBody := map[string]any{
		"actual_units":  actualUnits,
		"broker_job_id": brokerJobID,
		"work_unit":     brokerWorkUnit,
	}
	if encoded := header.Get("Livepeer-Settlement"); encoded != "" {
		if raw, decodeErr := base64.StdEncoding.DecodeString(encoded); decodeErr == nil {
			var settlement map[string]any
			if json.Unmarshal(raw, &settlement) == nil {
				settleBody["settlement"] = settlement
			}
		}
	}
	var settled JobSettleResponse
	if err := c.doWithRetry(ctx, http.MethodPost, job.SettleEndpoint, settleBody, &settled, 3); err != nil {
		c.telemetry.Emit(EmitTelemetryOptions{
			EventType:     "request.error",
			CorrelationID: requestID,
			Payload: map[string]interface{}{
				"phase":       "settle",
				"error_class": fmt.Sprintf("%T", err),
				"error_code":  errorCode(err),
			},
		})
		return nil, err
	}
	c.telemetry.Emit(EmitTelemetryOptions{
		EventType:     "request.settle_completed",
		CorrelationID: requestID,
		Payload: map[string]interface{}{
			"latency_ms":       time.Since(settleStarted).Milliseconds(),
			"refund_wei":       settled.RefundWei,
			"billed_value_wei": settled.BilledValueWei,
			"outcome":          settled.Outcome,
		},
	})
	c.telemetry.Emit(EmitTelemetryOptions{
		EventType:     "request.completed",
		CorrelationID: requestID,
		Payload: map[string]interface{}{
			"capability":       in.Capability,
			"offering":         in.Offering,
			"protocol":         job.Protocol,
			"transport":        job.Transport,
			"work_unit":        job.WorkUnit,
			"broker_job_id":    brokerJobID,
			"estimated_units":  in.EstimatedUnits,
			"actual_units":     settled.ActualUnits,
			"billed_value_wei": settled.BilledValueWei,
			"refund_wei":       settled.RefundWei,
			"outcome":          settled.Outcome,
			"broker_url":       job.BrokerURL,
		},
	})

	out := &JobResult{
		Status:         status,
		JobID:          settled.JobID,
		WorkID:         settled.WorkID,
		BrokerJobID:    brokerJobID,
		Protocol:       job.Protocol,
		Transport:      job.Transport,
		WorkUnit:       brokerWorkUnit,
		ActualUnits:    settled.ActualUnits,
		BilledValueWei: settled.BilledValueWei,
		RefundWei:      settled.RefundWei,
		Outcome:        settled.Outcome,
		CapStatus:      settled.CapStatus,
		RequestID:      job.RequestID,
		RawHeaders:     header,
		BodyText:       string(payload),
	}
	if strings.Contains(header.Get("Content-Type"), "json") && len(payload) > 0 {
		out.Body = json.RawMessage(payload)
	}
	return out, nil
}

// ---- sessions (case d) ----

// OpenSessionInput collects the arguments for OpenSession.
type OpenSessionInput struct {
	Capability           string
	Offering             string
	EstimatedRunwayUnits int64
	MaxTotalUnits        int64
}

// OpenSession opens a long-running session and returns the SessionHandle.
//
// in.MaxTotalUnits is the same input across all case-(d) modes, but
// the operational guarantee differs by mode class:
//
//	(d-bounded) modes (ws-realtime@v0):
//	  The session spends AT MOST MaxTotalUnits. It may end earlier;
//	  it ends no later than when this much is consumed. It cannot be
//	  extended — refills are not supported in these modes.
//
//	(d-extensible) modes (session-control-plus-media@v0,
//	rtmp-ingress-hls-egress@v0, live-session-remote-runner@v0,
//	live-session-gateway-ingest@v0):
//	  The session spends AT MOST MaxTotalUnits. Refills happen
//	  automatically within this ceiling; the session drains if a
//	  higher-tier cap (spend-period, operator-pool) is reached
//	  before MaxTotalUnits is exhausted.
//
// in.EstimatedRunwayUnits is the initial chunk LOC mints toward;
// SessionRunner tops up automatically as the broker signals
// balance-low.
//
// The caller is responsible for the broker-side WS / RTMP wire today
// (or use SessionRunner to drive it).
func (c *Client) OpenSession(ctx context.Context, in OpenSessionInput) (*SessionHandle, error) {
	c.emitSdkInitOnce()
	body := map[string]any{
		"capability":             in.Capability,
		"offering":               in.Offering,
		"estimated_runway_units": in.EstimatedRunwayUnits,
		"max_total_units":        in.MaxTotalUnits,
	}
	var out SessionHandle
	if err := c.do(ctx, http.MethodPost, "/v1/sessions", body, &out); err != nil {
		return nil, err
	}
	c.telemetry.Emit(EmitTelemetryOptions{
		EventType:     "session.opened",
		CorrelationID: out.SessionID,
		Payload: map[string]interface{}{
			"capability":           in.Capability,
			"offering":             in.Offering,
			"mode":                 out.Mode,
			"max_total_units":      in.MaxTotalUnits,
			"initial_runway_units": in.EstimatedRunwayUnits,
		},
	})
	return &out, nil
}

// RefillSession mints a top-up bound to an existing session. The caller
// is responsible for delivering the returned envelope to the broker via
// the mode-specific channel (control-WS frame or HTTP POST to topup_url).
func (c *Client) RefillSession(ctx context.Context, sessionID string, observedConsumedUnits *int64) (map[string]any, error) {
	c.telemetry.Emit(EmitTelemetryOptions{
		EventType:     "session.refill_requested",
		CorrelationID: sessionID,
	})
	refillStarted := time.Now()
	body := map[string]any{}
	if observedConsumedUnits != nil {
		body["observed_consumed_units"] = *observedConsumedUnits
	} else {
		body["observed_consumed_units"] = nil
	}
	var out map[string]any
	if err := c.do(ctx, http.MethodPost, "/v1/sessions/"+sessionID+"/refill", body, &out); err != nil {
		var locErr *Error
		if errors.As(err, &locErr) && locErr.Status == 402 {
			c.telemetry.Emit(EmitTelemetryOptions{
				EventType:     "session.refill_denied",
				CorrelationID: sessionID,
				Payload: map[string]interface{}{
					"which":         locErr.Details["which"],
					"remaining_wei": locErr.Details["remaining_wei"],
				},
			})
		} else {
			c.telemetry.Emit(EmitTelemetryOptions{
				EventType:     "session.error",
				CorrelationID: sessionID,
				Payload: map[string]interface{}{
					"phase":       "refill",
					"error_class": fmt.Sprintf("%T", err),
					"error_code":  errorCode(err),
				},
			})
		}
		return nil, err
	}
	c.telemetry.Emit(EmitTelemetryOptions{
		EventType:     "session.refill_granted",
		CorrelationID: sessionID,
		Payload: map[string]interface{}{
			"latency_ms":       time.Since(refillStarted).Milliseconds(),
			"refill_seq":       out["refill_seq"],
			"funded_value_wei": out["funded_value_wei"],
			"cap_status":       out["cap_status"],
		},
	})
	return out, nil
}

// CloseSession explicitly closes a session and finalizes accounting.
func (c *Client) CloseSession(ctx context.Context, sessionID string, actualUnits int64, outcome string, settlement map[string]any) (map[string]any, error) {
	body := map[string]any{"actual_units": actualUnits}
	if outcome != "" {
		body["outcome"] = outcome
	}
	if settlement != nil {
		body["settlement"] = settlement
	}
	var out map[string]any
	if err := c.do(ctx, http.MethodPost, "/v1/sessions/"+sessionID+"/close", body, &out); err != nil {
		c.telemetry.Emit(EmitTelemetryOptions{
			EventType:     "session.error",
			CorrelationID: sessionID,
			Payload: map[string]interface{}{
				"phase":       "close",
				"error_class": fmt.Sprintf("%T", err),
				"error_code":  errorCode(err),
			},
		})
		return nil, err
	}
	c.telemetry.Emit(EmitTelemetryOptions{
		EventType:     "session.closed",
		CorrelationID: sessionID,
		Payload: map[string]interface{}{
			"actual_units":     out["actual_units"],
			"billed_value_wei": out["billed_value_wei"],
			"refund_wei":       out["refund_wei"],
			"outcome":          out["outcome"],
			"closed_by":        "customer",
		},
	})
	return out, nil
}

// GetSessionStatus returns a read-only snapshot of a session.
func (c *Client) GetSessionStatus(ctx context.Context, sessionID string) (map[string]any, error) {
	var out map[string]any
	if err := c.do(ctx, http.MethodGet, "/v1/sessions/"+sessionID, nil, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// ---- internals ----

func readBroker(client *http.Client, req *http.Request) (int, http.Header, []byte, error) {
	res, err := client.Do(req)
	if err != nil {
		return 0, nil, nil, fmt.Errorf("openclearinghouse: broker call: %w", err)
	}
	defer func() { _ = res.Body.Close() }()
	payload, err := io.ReadAll(res.Body)
	if err != nil {
		return 0, nil, nil, fmt.Errorf("openclearinghouse: read broker body: %w", err)
	}
	// http-stream brokers report Livepeer-Work-Units as a *trailer*
	// rather than a header. Net/http exposes trailers on
	// res.Trailer ONLY after the body has been fully consumed. Merge
	// trailers into the returned Header so the caller can lookup
	// the field via the same `.Get("Livepeer-Work-Units")` regardless
	// of mode.
	merged := res.Header
	if len(res.Trailer) > 0 {
		merged = res.Header.Clone()
		for k, v := range res.Trailer {
			for _, vv := range v {
				merged.Add(k, vv)
			}
		}
	}
	return res.StatusCode, merged, payload, nil
}

func (c *Client) do(
	ctx context.Context,
	method, path string,
	body any,
	out any,
) error {
	return c.doWithHeaders(ctx, method, path, body, out, nil)
}

func (c *Client) doWithHeaders(
	ctx context.Context,
	method, path string,
	body any,
	out any,
	extraHeaders http.Header,
) error {
	var reader io.Reader
	if body != nil {
		buf, err := json.Marshal(body)
		if err != nil {
			return fmt.Errorf("openclearinghouse: marshal request body: %w", err)
		}
		reader = bytes.NewReader(buf)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, reader)
	if err != nil {
		return fmt.Errorf("openclearinghouse: build request: %w", err)
	}
	req.Header.Set("X-API-Key", c.apiKey)
	req.Header.Set("Livepeer-Open-Clearinghouse-SDK", c.sdkIdentity)
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	for name, values := range extraHeaders {
		for _, value := range values {
			req.Header.Add(name, value)
		}
	}

	res, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("openclearinghouse: do: %w", err)
	}
	defer func() { _ = res.Body.Close() }()
	payload, err := io.ReadAll(res.Body)
	if err != nil {
		return fmt.Errorf("openclearinghouse: read body: %w", err)
	}
	if res.StatusCode >= 200 && res.StatusCode < 300 {
		if out == nil || len(payload) == 0 {
			return nil
		}
		if err := json.Unmarshal(payload, out); err != nil {
			return fmt.Errorf("openclearinghouse: decode response: %w", err)
		}
		return nil
	}
	return parseError(res.StatusCode, res.Header.Get("Retry-After"), payload)
}

// doWithRetry wraps `do` with exponential backoff on transient
// failures. 5xx and 429 retry; 4xx surface immediately. Used by the
// settle path so a transient LOC blip doesn't leave a session
// unsettled — the janitor would catch it eventually, but synchronous
// retry buys low latency for the common case.
func (c *Client) doWithRetry(
	ctx context.Context,
	method, path string,
	body any,
	out any,
	maxRetries int,
) error {
	if maxRetries < 1 {
		maxRetries = 1
	}
	backoff := 500 * time.Millisecond
	var lastErr error
	for attempt := 1; attempt <= maxRetries; attempt++ {
		err := c.do(ctx, method, path, body, out)
		if err == nil {
			return nil
		}
		var locErr *Error
		if errors.As(err, &locErr) {
			if locErr.Status < 500 && locErr.Status != http.StatusTooManyRequests {
				return err
			}
		}
		lastErr = err
		if attempt >= maxRetries {
			break
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(backoff):
		}
		backoff *= 2
	}
	return lastErr
}

func parseError(status int, retryAfter string, payload []byte) *Error {
	out := &Error{Status: status, Details: map[string]any{}}
	var dict map[string]any
	if len(payload) > 0 {
		_ = json.Unmarshal(payload, &dict)
	}
	if envelope, ok := dict["error"].(map[string]any); ok {
		if code, ok := envelope["code"].(string); ok {
			out.Code = code
		}
		if msg, ok := envelope["message"].(string); ok {
			out.Message = msg
		}
		if det, ok := envelope["details"].(map[string]any); ok {
			out.Details = det
		}
	} else if d, ok := dict["detail"].(string); ok {
		out.Code = d
		out.Message = d
	}
	if out.Message == "" {
		out.Message = fmt.Sprintf("HTTP %d", status)
	}
	if n, err := strconv.Atoi(retryAfter); err == nil {
		out.RetryAfterSeconds = n
	}
	return out
}

// newUUIDv4 generates a v4 UUID without pulling in google/uuid.
func newUUIDv4() string {
	var b [16]byte
	_, _ = cryptorand.Read(b[:])
	b[6] = (b[6] & 0x0f) | 0x40 // version 4
	b[8] = (b[8] & 0x3f) | 0x80 // variant 10
	return fmt.Sprintf(
		"%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
		b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7],
		b[8], b[9], b[10], b[11], b[12], b[13], b[14], b[15],
	)
}
