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
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
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
	"unicode/utf8"
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
	SDKVersion = "2.0.0"
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
	JobID              string `json:"job_id"`
	RequestID          string `json:"request_id"`
	WorkID             string `json:"work_id"`
	BrokerURL          string `json:"broker_url"`
	Protocol           string `json:"protocol"`
	Transport          string `json:"transport"`
	WorkUnit           string `json:"work_unit"`
	SpendAuthorization string `json:"spend_authorization"`
	AccountingMode     string `json:"accounting_mode"`
	ExpectedValueWei   Wei    `json:"expected_value_wei"`
	FundedValueWei     Wei    `json:"funded_value_wei"`
	SettleEndpoint     string `json:"settle_endpoint"`
	OpenedAt           string `json:"opened_at"`
	// RouteSnapshot is the route LOC bound the job to (v2 gateways).
	// SubmitJob reads `extra.openai.model` from it; see SubmitJobInput.Body.
	RouteSnapshot map[string]any `json:"route_snapshot,omitempty"`
}

// JobSettleResponse mirrors POST /v1/jobs/{id}/settle response.
type JobSettleResponse struct {
	JobID          string    `json:"job_id"`
	WorkID         string    `json:"work_id"`
	ActualUnits    int64     `json:"actual_units"`
	BilledValueWei Wei       `json:"billed_value_wei"`
	RefundWei      Wei       `json:"refund_wei"`
	Outcome        string    `json:"outcome"`
	ClosedAt       string    `json:"closed_at"`
	CapStatus      CapStatus `json:"cap_status"`
}

// JobStatusResponse preserves LOC's four accounting outcomes without
// representing a conservative charge or non-admission as broker settlement.
// BilledValueWei is nil (Wei.IsNil) until LOC has billed the job.
type JobStatusResponse struct {
	JobID                                 string  `json:"job_id"`
	RequestID                             string  `json:"request_id"`
	WorkID                                string  `json:"work_id"`
	State                                 string  `json:"state"`
	AccountingOutcome                     string  `json:"accounting_outcome"`
	BrokerExchangeOutcome                 *string `json:"broker_exchange_outcome"`
	ActualUnits                           *int64  `json:"actual_units"`
	BilledValueWei                        Wei     `json:"billed_value_wei"`
	FundedValueWei                        Wei     `json:"funded_value_wei"`
	OpenedAt                              string  `json:"opened_at"`
	ClosedAt                              *string `json:"closed_at"`
	CreationRound                         *int64  `json:"creation_round"`
	ExpiresAfterRound                     *int64  `json:"expires_after_round"`
	MintTicketValidityPeriod              *int64  `json:"mint_ticket_validity_period"`
	MintTicketValidityPeriodObservedAt    *string `json:"mint_ticket_validity_period_observed_at"`
	ObservedCurrentRound                  *int64  `json:"observed_current_round"`
	CurrentTicketValidityPeriod           *int64  `json:"current_ticket_validity_period"`
	CurrentTicketValidityPeriodObservedAt *string `json:"current_ticket_validity_period_observed_at"`
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
	BilledValueWei Wei
	RefundWei      Wei
	Outcome        string
	CapStatus      CapStatus
	RequestID      string
	RawHeaders     http.Header
}

// SessionHandle is the outbound from OpenSession (case d). Carries the
// broker URL + scoped authorization; the caller drives the broker WS/RTMP
// wire today.
type SessionHandle struct {
	SessionID          string                       `json:"session_id"`
	RequestID          string                       `json:"request_id"`
	WorkID             string                       `json:"work_id"`
	BrokerURL          string                       `json:"broker_url"`
	Protocol           string                       `json:"protocol"`
	Capability         string                       `json:"-"`
	Offering           string                       `json:"-"`
	Session            SessionAxes                  `json:"session"`
	SessionParams      map[string]any               `json:"-"`
	SpendAuthorization string                       `json:"spend_authorization"`
	AccountingMode     string                       `json:"accounting_mode"`
	CallerProof        string                       `json:"-"`
	SessionOpenBody    []byte                       `json:"-"`
	MaxTotalUnits      int64                        `json:"-"`
	SignCallerProof    func([]byte) (string, error) `json:"-"`
	ExpectedValueWei   Wei                          `json:"expected_value_wei"`
	FundedValueWei     Wei                          `json:"funded_value_wei"`
	RefillEndpoint     string                       `json:"refill_endpoint"`
	CloseEndpoint      string                       `json:"close_endpoint"`
	OpenedAt           string                       `json:"opened_at"`
}

type SessionAxes struct {
	DescriptorSchema string `json:"descriptor_schema"`
	Attachment       string `json:"attachment"`
	Metering         string `json:"metering"`
	Refill           string `json:"refill"`
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
	// Body is forwarded to the broker as-is; callers marshal JSON
	// themselves. Its exact bytes are hashed into the scoped authorization,
	// so SubmitJob never mutates them after LOC locks the route.
	Body            []byte
	ContentType     string // defaults to application/json if Body starts with {/[, else octet-stream
	MaxTotalUnits   int64  // optional; defaults to EstimatedUnits
	RequestID       string // optional; SubmitJob generates a UUID if empty
	Transport       string // unary (default), stream, or multipart
	Timeout         time.Duration
	CallerPublicKey string
	SignCallerProof func([]byte) (string, error)
}

func signCallerAuthorization(encoded string, signer func([]byte) (string, error)) (string, error) {
	if encoded == "" {
		return "", nil
	}
	if signer == nil {
		return "", &BrokerProtocolError{Code: "caller_proof_signer_required", Message: "LOC returned a spend authorization but no caller-proof signer was supplied"}
	}
	raw, err := base64.StdEncoding.Strict().DecodeString(encoded)
	if err != nil {
		return "", &BrokerProtocolError{Code: "broker_protocol_error", Message: "LOC returned a malformed spend authorization"}
	}
	proof, err := signer(raw)
	if err != nil {
		return "", fmt.Errorf("openclearinghouse: caller-proof signer: %w", err)
	}
	if proof == "" {
		return "", &BrokerProtocolError{Code: "caller_proof_signer_failed", Message: "caller-proof signer returned an empty proof"}
	}
	return proof, nil
}

func normalizedTransport(transport string) string {
	if transport == "" {
		return "unary"
	}
	return transport
}

// SubmitJob is the load-bearing convenience method: opens a job via
// POST /v1/jobs, calls the broker with a scoped authorization and caller
// proof, reads Livepeer-Work-Units
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
	if in.CallerPublicKey == "" || in.SignCallerProof == nil {
		return nil, &BrokerProtocolError{Code: "caller_proof_scope_invalid", Message: "CallerPublicKey and SignCallerProof are required"}
	}
	digest := sha256.Sum256(in.Body)
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
		"capability":              in.Capability,
		"offering":                in.Offering,
		"transport":               transport,
		"estimated_units":         in.EstimatedUnits,
		"workload_request_digest": hex.EncodeToString(digest[:]),
		"caller_public_key":       in.CallerPublicKey,
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
			"funded_value_wei": weiTelemetry(job.FundedValueWei),
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
	accountingMode := job.AccountingMode
	brokerBody := in.Body

	brokerCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	req, err := http.NewRequestWithContext(brokerCtx, http.MethodPost, endpoint, bytes.NewReader(brokerBody))
	if err != nil {
		return nil, fmt.Errorf("openclearinghouse: build broker request: %w", err)
	}
	req.Header.Set("Livepeer-Capability", in.Capability)
	req.Header.Set("Livepeer-Offering", in.Offering)
	if accountingMode != "wholesale_account" {
		return nil, &BrokerProtocolError{Code: "protocol_unsupported", Message: fmt.Sprintf("LOC returned unsupported accounting mode %q", accountingMode)}
	}
	proof, proofErr := signCallerAuthorization(job.SpendAuthorization, in.SignCallerProof)
	if proofErr != nil {
		return nil, proofErr
	}
	if job.SpendAuthorization == "" || proof == "" {
		return nil, &BrokerProtocolError{Code: "broker_protocol_error", Message: "LOC returned an incomplete wholesale authorization"}
	}
	req.Header.Set("Livepeer-Authorization", job.SpendAuthorization)
	req.Header.Set("Livepeer-Caller-Proof", proof)
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
	initialJobID := header.Get("Livepeer-Job-Id")
	if transport == "stream" && initialJobID == "" {
		return nil, &BrokerProtocolError{
			Code: "broker_protocol_error", Message: "stream response missing Livepeer-Job-Id", Status: status,
			Details: map[string]any{"missing_headers": []string{"Livepeer-Job-Id"}},
		}
	}

	// 3. Read the terminal claim. Unary/multipart usually carry it in the
	// response headers; streams carry it in trailers, which proxies in
	// front of brokers frequently strip. Whenever the claim is incomplete,
	// recover it through the broker's durable request-id lookup — the one
	// path the caller cannot withhold (mirrors the Python/TS SDKs).
	claimHeader, claimStatus := header, status
	var claimBody map[string]any
	if header.Get("Livepeer-Work-Units") == "" || header.Get("Livepeer-Work-Unit") == "" ||
		initialJobID == "" || header.Get("Livepeer-Settlement") == "" {
		exStatus, exHeader, exBody, exErr := c.lookupExchange(ctx, job.BrokerURL, job.RequestID)
		if exErr != nil {
			return nil, exErr
		}
		claimHeader, claimStatus, claimBody = exHeader, exStatus, exBody
	}
	workUnits := claimHeader.Get("Livepeer-Work-Units")
	brokerWorkUnit := claimHeader.Get("Livepeer-Work-Unit")
	brokerJobID := claimHeader.Get("Livepeer-Job-Id")
	if claimBody != nil {
		if workUnits == "" {
			workUnits = jsonScalarString(claimBody["work_units"])
		}
		if brokerWorkUnit == "" {
			brokerWorkUnit = jsonScalarString(claimBody["unit"])
		}
		if brokerJobID == "" {
			brokerJobID = jsonScalarString(claimBody["job_id"])
		}
	}
	var missing []string
	for _, field := range []struct{ name, value string }{
		{"Livepeer-Work-Units", workUnits},
		{"Livepeer-Work-Unit", brokerWorkUnit},
		{"Livepeer-Job-Id", brokerJobID},
	} {
		if field.value == "" {
			missing = append(missing, field.name)
		}
	}
	if len(missing) > 0 {
		return nil, &BrokerProtocolError{
			Code: "broker_protocol_error", Status: claimStatus,
			Message: "terminal broker response missing required headers: " + strings.Join(missing, ", "),
			Details: map[string]any{"missing_headers": missing},
		}
	}
	if initialJobID != "" && brokerJobID != initialJobID {
		return nil, &BrokerProtocolError{
			Code: "broker_job_id_mismatch", Status: claimStatus,
			Message: fmt.Sprintf("settlement query returned job id %q; expected %q", brokerJobID, initialJobID),
			Details: map[string]any{"expected": initialJobID, "received": brokerJobID},
		}
	}
	actualUnits, parseErr := strconv.ParseInt(workUnits, 10, 64)
	if parseErr != nil || actualUnits < 0 {
		return nil, &BrokerProtocolError{Code: "broker_protocol_error", Message: "invalid Livepeer-Work-Units", Status: claimStatus}
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
	encoded := claimHeader.Get("Livepeer-Settlement")
	var bodySettlement string
	if claimBody != nil {
		bodySettlement = jsonScalarString(claimBody["settlement"])
	}
	if encoded != "" && bodySettlement != "" && encoded != bodySettlement {
		return nil, &BrokerProtocolError{Code: "broker_protocol_error", Message: "broker exchange settlement header and body disagree", Status: claimStatus}
	}
	if encoded == "" {
		encoded = bodySettlement
	}
	if encoded == "" {
		encoded = header.Get("Livepeer-Settlement")
	}
	if encoded == "" {
		return nil, &BrokerProtocolError{
			Code: "broker_protocol_error", Message: "terminal response missing Livepeer-Settlement", Status: claimStatus,
			Details: map[string]any{"missing_headers": []string{"Livepeer-Settlement"}},
		}
	}
	raw, decodeErr := base64.StdEncoding.DecodeString(encoded)
	if decodeErr != nil {
		return nil, &BrokerProtocolError{Code: "broker_protocol_error", Message: "terminal response has malformed Livepeer-Settlement", Status: claimStatus}
	}
	var settlement map[string]any
	if jsonErr := json.Unmarshal(raw, &settlement); jsonErr != nil || settlement == nil {
		return nil, &BrokerProtocolError{Code: "broker_protocol_error", Message: "terminal response has malformed Livepeer-Settlement", Status: claimStatus}
	}
	settleBody["settlement"] = settlement
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
			"refund_wei":       weiTelemetry(settled.RefundWei),
			"billed_value_wei": weiTelemetry(settled.BilledValueWei),
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
			"billed_value_wei": weiTelemetry(settled.BilledValueWei),
			"refund_wei":       weiTelemetry(settled.RefundWei),
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
	DescriptorSchema     string
	SessionParams        map[string]any
	EstimatedRunwayUnits int64
	MaxTotalUnits        int64
	RequestID            string
	CallerPublicKey      string
	SignCallerProof      func([]byte) (string, error)
}

// OpenSession opens a long-running session and returns the SessionHandle.
//
// MaxTotalUnits is a hard spend ceiling. Whether the session can extend
// within that ceiling comes from the offering's session.refill axis:
// bounded drains without refilling; extensible uses the broker's
// authoritative HTTP top-up contract.
//
// in.EstimatedRunwayUnits is the initial chunk LOC mints toward;
// SessionRunner tops up automatically as the broker reports a normative
// low balance.
//
// The caller is responsible for the broker-side WS / RTMP wire today
// (or use SessionRunner to drive it).
func (c *Client) OpenSession(ctx context.Context, in OpenSessionInput) (*SessionHandle, error) {
	c.emitSdkInitOnce()
	if in.RequestID == "" {
		in.RequestID = newUUIDv4()
	}
	if in.CallerPublicKey == "" || in.SignCallerProof == nil {
		return nil, &BrokerProtocolError{Code: "caller_proof_scope_invalid", Message: "CallerPublicKey and SignCallerProof are required"}
	}
	type preparation struct {
		GatewaySessionID string         `json:"gateway_session_id"`
		RouteBinding     map[string]any `json:"route_binding"`
		PreparationToken string         `json:"preparation_token"`
	}
	var prepared preparation
	prepareBody := map[string]any{"capability": in.Capability, "offering": in.Offering, "descriptor_schema": in.DescriptorSchema}
	if err := c.doWithHeaders(ctx, http.MethodPost, "/v1/sessions/prepare", prepareBody, &prepared, http.Header{"Idempotency-Key": []string{in.RequestID + ":prepare"}}); err != nil {
		return nil, err
	}
	sessionOpenBody, err := json.Marshal(map[string]any{"gateway_session_id": prepared.GatewaySessionID, "session_params": in.SessionParams})
	if err != nil {
		return nil, fmt.Errorf("openclearinghouse: encode session commitment: %w", err)
	}
	body := map[string]any{
		"capability":             in.Capability,
		"offering":               in.Offering,
		"descriptor_schema":      in.DescriptorSchema,
		"session_params":         in.SessionParams,
		"estimated_runway_units": in.EstimatedRunwayUnits,
		"max_total_units":        in.MaxTotalUnits,
	}
	digest := sha256.Sum256(sessionOpenBody)
	body["gateway_session_id"] = prepared.GatewaySessionID
	body["preparation_token"] = prepared.PreparationToken
	body["route_binding"] = prepared.RouteBinding
	body["workload_request_digest"] = hex.EncodeToString(digest[:])
	body["caller_public_key"] = in.CallerPublicKey
	var out SessionHandle
	headers := http.Header{"Idempotency-Key": []string{in.RequestID}}
	if err := c.doWithHeaders(ctx, http.MethodPost, "/v1/sessions", body, &out, headers); err != nil {
		return nil, err
	}
	if out.Protocol != "paid-session/v1" {
		return nil, fmt.Errorf("openclearinghouse: unsupported session protocol %q", out.Protocol)
	}
	if out.Session.DescriptorSchema != in.DescriptorSchema {
		return nil, fmt.Errorf("openclearinghouse: descriptor schema mismatch")
	}
	proof, err := signCallerAuthorization(out.SpendAuthorization, in.SignCallerProof)
	if err != nil {
		return nil, err
	}
	if out.AccountingMode != "wholesale_account" {
		return nil, &BrokerProtocolError{Code: "protocol_unsupported", Message: fmt.Sprintf("LOC returned unsupported accounting mode %q", out.AccountingMode)}
	}
	if out.SpendAuthorization == "" || proof == "" {
		return nil, &BrokerProtocolError{Code: "broker_protocol_error", Message: "LOC returned an incomplete wholesale authorization"}
	}
	out.Capability = in.Capability
	out.Offering = in.Offering
	out.SessionParams = in.SessionParams
	out.CallerProof = proof
	out.SessionOpenBody = sessionOpenBody
	out.MaxTotalUnits = in.MaxTotalUnits
	out.SignCallerProof = in.SignCallerProof
	c.telemetry.Emit(EmitTelemetryOptions{
		EventType:     "session.opened",
		CorrelationID: out.SessionID,
		Payload: map[string]interface{}{
			"capability":           in.Capability,
			"offering":             in.Offering,
			"protocol":             out.Protocol,
			"descriptor_schema":    out.Session.DescriptorSchema,
			"refill":               out.Session.Refill,
			"max_total_units":      in.MaxTotalUnits,
			"initial_runway_units": in.EstimatedRunwayUnits,
		},
	})
	return &out, nil
}

// ReviseSessionAuthorization explicitly increases a wholesale session's
// cumulative cap. The digest must bind the exact body sent to the broker.
func (c *Client) ReviseSessionAuthorization(ctx context.Context, sessionID string, observedConsumedUnits int64, maxTotalUnits int64, requestDigest, requestID string) (map[string]any, error) {
	body := map[string]any{
		"observed_consumed_units": observedConsumedUnits,
		"max_total_units":         maxTotalUnits,
		"workload_request_digest": requestDigest,
	}
	if requestID == "" {
		requestID = newUUIDv4()
	}
	var out map[string]any
	err := c.doWithHeaders(ctx, http.MethodPost, "/v1/sessions/"+sessionID+"/refill", body, &out, http.Header{"Idempotency-Key": []string{requestID}})
	return out, err
}

// CloseSession explicitly closes a session and finalizes accounting.
func (c *Client) CloseSession(ctx context.Context, sessionID string, actualUnits int64, outcome string, settlement map[string]any) (map[string]any, error) {
	body := map[string]any{"actual_units": actualUnits}
	if outcome != "" {
		body["outcome"] = outcome
	}
	if settlement == nil {
		return nil, fmt.Errorf("openclearinghouse: settlement is required")
	}
	body["settlement"] = settlement
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
			"billed_value_wei": weiTelemetryAny(out["billed_value_wei"]),
			"refund_wei":       weiTelemetryAny(out["refund_wei"]),
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

// GetJobStatus returns exact, conservative, unresolved, or audit-only billing state.
func (c *Client) GetJobStatus(ctx context.Context, jobID string) (*JobStatusResponse, error) {
	var out JobStatusResponse
	if err := c.do(ctx, http.MethodGet, "/v1/jobs/"+jobID, nil, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// ---- internals ----

const (
	exchangeLookupAttempts    = 8
	exchangeLookupBaseBackoff = 50 * time.Millisecond
)

// lookupExchange polls GET {broker_url}/v1/exchange/{request_id} until the
// broker reports a SETTLED outcome (200), backing off 50ms*2^attempt while
// it is still IN_FLIGHT / ACCOUNTING_PENDING (202). Any other status or
// outcome is unresolvable.
func (c *Client) lookupExchange(ctx context.Context, brokerURL, requestID string) (int, http.Header, map[string]any, error) {
	exchangeURL := strings.TrimRight(brokerURL, "/") + "/v1/exchange/" + url.PathEscape(requestID)
	for attempt := 0; attempt < exchangeLookupAttempts; attempt++ {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, exchangeURL, nil)
		if err != nil {
			return 0, nil, nil, fmt.Errorf("openclearinghouse: build broker exchange request: %w", err)
		}
		req.Header.Set("Accept", "application/json")
		res, err := c.http.Do(req)
		if err != nil {
			return 0, nil, nil, fmt.Errorf("openclearinghouse: broker exchange lookup: %w", err)
		}
		raw, readErr := io.ReadAll(res.Body)
		_ = res.Body.Close()
		if readErr != nil {
			return 0, nil, nil, fmt.Errorf("openclearinghouse: read broker exchange body: %w", readErr)
		}
		var exchange map[string]any
		if json.Unmarshal(raw, &exchange) != nil || exchange == nil {
			return 0, nil, nil, &BrokerProtocolError{Code: "broker_protocol_error", Message: "broker exchange lookup returned malformed JSON", Status: res.StatusCode}
		}
		if got, _ := exchange["request_id"].(string); got != requestID {
			return 0, nil, nil, &BrokerProtocolError{
				Code: "broker_request_id_mismatch", Message: "broker exchange lookup returned a different request id", Status: res.StatusCode,
				Details: map[string]any{"expected": requestID, "received": exchange["request_id"]},
			}
		}
		outcome, _ := exchange["outcome"].(string)
		if res.StatusCode == http.StatusAccepted && (outcome == "IN_FLIGHT" || outcome == "ACCOUNTING_PENDING") {
			if attempt < exchangeLookupAttempts-1 {
				select {
				case <-ctx.Done():
					return 0, nil, nil, ctx.Err()
				case <-time.After(exchangeLookupBaseBackoff * (1 << uint(attempt))):
				}
				continue
			}
			return 0, nil, nil, &BrokerProtocolError{
				Code: "broker_exchange_pending", Message: "broker exchange remained " + outcome, Status: res.StatusCode,
				Details: map[string]any{"outcome": outcome},
			}
		}
		if res.StatusCode != http.StatusOK || outcome != "SETTLED" {
			return 0, nil, nil, &BrokerProtocolError{
				Code: "broker_exchange_unresolved", Message: fmt.Sprintf("broker exchange lookup returned %q", outcome), Status: res.StatusCode,
				Details: map[string]any{"outcome": outcome},
			}
		}
		return res.StatusCode, res.Header, exchange, nil
	}
	return 0, nil, nil, &BrokerProtocolError{Code: "broker_exchange_pending", Message: "broker exchange lookup exhausted retries"}
}

// jsonScalarString renders a decoded JSON scalar as the string the
// equivalent header would carry ("" for nil / non-scalars).
func jsonScalarString(v any) string {
	switch t := v.(type) {
	case nil:
		return ""
	case string:
		return t
	case float64:
		return strconv.FormatFloat(t, 'f', -1, 64)
	case bool:
		return strconv.FormatBool(t)
	case json.Number:
		return t.String()
	}
	return ""
}

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
// settle path so a transient LOC blip doesn't leave a job unsettled.
// The retry preserves the broker-signed terminal claim across that window.
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
	} else if d, ok := dict["detail"]; ok && d != nil {
		if s, ok := d.(string); ok {
			out.Code = s
			out.Message = s
		} else {
			// FastAPI validation bodies carry `detail` as a list (or an
			// object): {"detail":[{"type":"missing","loc":[...],"msg":...}]}.
			// There is no canonical code; surface the compact JSON as the
			// message and keep the decoded value for callers.
			out.Details["detail"] = d
			if enc, err := json.Marshal(d); err == nil {
				out.Message = truncateRunes(string(enc), errorMessageMaxRunes)
			}
		}
	}
	if out.Message == "" {
		out.Message = fmt.Sprintf("HTTP %d", status)
	}
	if n, err := strconv.Atoi(retryAfter); err == nil {
		out.RetryAfterSeconds = n
	}
	return out
}

// errorMessageMaxRunes bounds the synthesized message for validation
// bodies so a huge `detail` list doesn't end up in logs verbatim.
const errorMessageMaxRunes = 500

// truncateRunes keeps the first max runes, ending with "..." when it cut
// (same shape as the Python SDK, so messages match across SDKs).
func truncateRunes(s string, max int) string {
	if utf8.RuneCountInString(s) <= max {
		return s
	}
	runes := []rune(s)
	return string(runes[:max-3]) + "..."
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
