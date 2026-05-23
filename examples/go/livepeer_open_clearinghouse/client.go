// Package openclearinghouse is a reference Go SDK for the Livepeer
// Open Clearinghouse payment clearinghouse. It wraps the few HTTP
// endpoints app developers need: discovery, payment mint + SubmitJob,
// and usage reconciliation.
//
// Construct one Client per process and reuse it. The zero value is not
// useful — always go through NewClient.
//
// # Wire-shape source of truth
//
// The response types defined below (Mint, Capability, Orchestrator, …)
// are mirrored by the oapi-codegen output in _generated.go, which is
// regenerated from the gateway's /openapi.json. The hand-typed
// versions exist for ergonomics (no openapi_types.UUID leakage in
// public signatures, idiomatic Go field names); _generated.go lives
// alongside as a drift-detection target — diff the two when the
// gateway evolves the schema.
//
// Regen recipe (from repo root):
//
//	make refresh-openapi
//
// then from this directory:
//
//	oapi-codegen -config /tmp/oapi-codegen.yaml /tmp/openapi-3.0.json
//
// where /tmp/openapi-3.0.json is the 3.1→3.0 down-converted spec (the
// Makefile target prints the exact shell snippet).
package openclearinghouse

import (
	"bytes"
	"context"
	cryptorand "crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

// Mint is the response from POST /v1/payments/mint.
// PaymentBytes is base64; pass it verbatim in the "Livepeer-Payment"
// header on your request to the orchestrator.
type Mint struct {
	PaymentID           string `json:"payment_id"`
	WorkID              string `json:"work_id"`
	PaymentBytes        string `json:"payment_bytes"`
	ExpectedValueWei    string `json:"expected_value_wei"`
	FundedValueWei      string `json:"funded_value_wei"`
	RecipientEthAddress string `json:"recipient_eth_address"`
}

// Capability bundles a name with its work_unit and offerings.
type Capability struct {
	Name      string     `json:"name"`
	WorkUnit  string     `json:"work_unit"`
	Offerings []Offering `json:"offerings"`
}

// Offering describes one variant of a capability (e.g. a specific model)
// with its price per work-unit.
type Offering struct {
	ID                  string `json:"id"`
	PricePerWorkUnitWei string `json:"price_per_work_unit_wei"`
	WorkUnit            string `json:"work_unit"`
}

// Orchestrator is one entry returned by GET /v1/orchestrators.
// The `capabilities` field is a nested list of Capability objects
// (the orch's full advertisement) — not a flat list of capability names.
type Orchestrator struct {
	EthAddress      string       `json:"eth_address"`
	WorkerURL       string       `json:"worker_url"`
	Capabilities    []Capability `json:"capabilities"`
	SignatureStatus string       `json:"signature_status"`
	FreshnessStatus string       `json:"freshness_status"`
}

// UsageReportResult is the response from POST /v1/usage/report. The
// gateway refunds the difference between the funded amount and what
// the orchestrator actually consumed.
type UsageReportResult struct {
	RefundedWei   string `json:"refunded_wei"`
	PaymentStatus string `json:"payment_status"`
	NewBalanceWei string `json:"new_balance_wei"`
	Usage         struct {
		ID              string `json:"id"`
		ActualWorkUnits int    `json:"actual_work_units"`
		FinalChargeWei  string `json:"final_charge_wei"`
	} `json:"usage"`
}

// Client wraps an *http.Client. Zero allocations on the hot path.
type Client struct {
	baseURL string
	apiKey  string
	http    *http.Client
}

// Options is the input to NewClient.
type Options struct {
	BaseURL string
	APIKey  string
	// Optional. Pass an *http.Client with your own timeouts/transport.
	// Defaults to one with a 15s timeout.
	HTTP *http.Client
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
	return &Client{
		baseURL: strings.TrimRight(opts.BaseURL, "/"),
		apiKey:  opts.APIKey,
		http:    httpClient,
	}, nil
}

// ---- discovery ----

// ListCapabilities returns the capability catalog the gateway is currently
// advertising via service-registry-daemon.
func (c *Client) ListCapabilities(ctx context.Context) ([]Capability, error) {
	var resp struct {
		Items []Capability `json:"items"`
	}
	if err := c.do(ctx, http.MethodGet, "/v1/capabilities", nil, "", &resp); err != nil {
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
	if err := c.do(ctx, http.MethodGet, path, nil, "", &resp); err != nil {
		return nil, err
	}
	return resp.Items, nil
}

// ---- payments ----

// MintPaymentInput collects the arguments for MintPayment.
type MintPaymentInput struct {
	Capability     string
	Offering       string
	WorkUnits      int
	IdempotencyKey string
}

// MintPayment is the load-bearing call. Returns a signed payment ticket
// you pass to the orchestrator in the Livepeer-Payment header.
func (c *Client) MintPayment(ctx context.Context, in MintPaymentInput) (*Mint, error) {
	body := map[string]any{
		"capability": in.Capability,
		"offering":   in.Offering,
		"work_units": in.WorkUnits,
	}
	var out Mint
	if err := c.do(ctx, http.MethodPost, "/v1/payments/mint", body, in.IdempotencyKey, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// ReportUsageInput collects the arguments for ReportUsage.
type ReportUsageInput struct {
	PaymentID       string
	ActualWorkUnits int
	IdempotencyKey  string
}

// RouteView mirrors GET /v1/routes for a (capability, offering).
type RouteView struct {
	EthAddress          string `json:"eth_address"`
	WorkerURL           string `json:"worker_url"`
	Capability          string `json:"capability"`
	Offering            string `json:"offering"`
	PricePerWorkUnitWei string `json:"price_per_work_unit_wei"`
}

// JobResult is the return shape of SubmitJob — the orchestrator's
// response wrapped with the side-channel info from the mint round-trip.
type JobResult struct {
	// Body is the orchestrator's response body. JSON when the
	// Content-Type indicates it, otherwise the raw bytes.
	Body                json.RawMessage
	BodyText            string
	Status              int
	PaymentID           string
	RecipientEthAddress string
	RequestID           string
	RawHeaders          http.Header
}

// SubmitJobInput collects the arguments for SubmitJob.
type SubmitJobInput struct {
	Capability     string
	Offering       string
	WorkUnits      int
	Body           []byte // raw bytes; caller marshals JSON if needed
	ContentType    string // defaults to application/json if Body looks like JSON, octet-stream otherwise
	IdempotencyKey string
	RequestID      string // optional; SubmitJob generates a UUID if empty
	Mode           string // defaults to "http-reqresp@v0"
	SpecVersion    string // defaults to "0.1"
	Timeout        time.Duration
}

// SubmitJob is the load-bearing convenience method: route selection +
// payment mint + orch HTTP call with the canonical POST <broker>/v1/cap
// shape and the five Livepeer headers.
//
// Don't put a "model" field in OpenAI-shaped bodies — the orchestrator
// routes via Livepeer-Offering and most upstreams (vLLM, etc.) will
// 404 on a mismatched model name. The offering identifies the model.
func (c *Client) SubmitJob(ctx context.Context, in SubmitJobInput) (*JobResult, error) {
	// 1. Route — first orch advertising this offering.
	var route RouteView
	routePath := "/v1/routes?capability=" + url.QueryEscape(in.Capability) +
		"&offering=" + url.QueryEscape(in.Offering)
	if err := c.do(ctx, http.MethodGet, routePath, nil, "", &route); err != nil {
		return nil, err
	}

	// Defaults for the orch call. Resolved once; reused for both attempts.
	requestID := in.RequestID
	if requestID == "" {
		requestID = newUUIDv4()
	}
	mode := in.Mode
	if mode == "" {
		mode = "http-reqresp@v0"
	}
	specVersion := in.SpecVersion
	if specVersion == "" {
		specVersion = "0.1"
	}
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
	endpoint := strings.TrimRight(route.WorkerURL, "/") + "/v1/cap"

	// attemptOnce mints fresh + POSTs to the orch. Returns status,
	// headers, body, mint — `*http.Response` stays inside the closure so
	// the body is closed deterministically (bodyclose-friendly). The
	// retry on INVALID_RECIPIENT_RAND MUST mint a new ticket; replaying
	// the rejected one would just be rejected again. The retry also burns
	// a fresh Idempotency-Key so the gateway's mint-idempotency ledger
	// doesn't replay the rejected attempt.
	attemptOnce := func(retry bool) (*Mint, int, http.Header, []byte, error) {
		idemp := in.IdempotencyKey
		if retry {
			idemp = ""
		}
		mint, err := c.MintPayment(ctx, MintPaymentInput{
			Capability:     in.Capability,
			Offering:       in.Offering,
			WorkUnits:      in.WorkUnits,
			IdempotencyKey: idemp,
		})
		if err != nil {
			return nil, 0, nil, nil, err
		}
		orchCtx, cancel := context.WithTimeout(ctx, timeout)
		defer cancel()
		req, err := http.NewRequestWithContext(orchCtx, http.MethodPost, endpoint, bytes.NewReader(in.Body))
		if err != nil {
			return nil, 0, nil, nil, fmt.Errorf("livepeer_open_clearinghouse: build orch request: %w", err)
		}
		req.Header.Set("Livepeer-Capability", in.Capability)
		req.Header.Set("Livepeer-Offering", in.Offering)
		req.Header.Set("Livepeer-Payment", mint.PaymentBytes)
		req.Header.Set("Livepeer-Mode", mode)
		req.Header.Set("Livepeer-Spec-Version", specVersion)
		req.Header.Set("Livepeer-Request-Id", requestID)
		req.Header.Set("Content-Type", contentType)

		res, err := c.http.Do(req)
		if err != nil {
			return nil, 0, nil, nil, fmt.Errorf("livepeer_open_clearinghouse: orch call: %w", err)
		}
		defer func() { _ = res.Body.Close() }()
		payload, err := io.ReadAll(res.Body)
		if err != nil {
			return nil, 0, nil, nil, fmt.Errorf("livepeer_open_clearinghouse: read orch body: %w", err)
		}
		return mint, res.StatusCode, res.Header, payload, nil
	}

	mint, status, header, payload, err := attemptOnce(false)
	if err != nil {
		return nil, err
	}

	// Orch session rotation: 401 + INVALID_RECIPIENT_RAND → mint fresh,
	// retry once.
	if status == http.StatusUnauthorized && bytes.Contains(payload, []byte("INVALID_RECIPIENT_RAND")) {
		mint, status, header, payload, err = attemptOnce(true)
		if err != nil {
			return nil, err
		}
	}

	out := &JobResult{
		Status:              status,
		PaymentID:           mint.PaymentID,
		RecipientEthAddress: mint.RecipientEthAddress,
		RequestID:           requestID,
		RawHeaders:          header,
		BodyText:            string(payload),
	}
	if strings.Contains(header.Get("Content-Type"), "json") && len(payload) > 0 {
		out.Body = json.RawMessage(payload)
	}
	return out, nil
}

// ReportUsage tells the gateway how many work units the orchestrator
// actually consumed so it can refund the unused portion of the funded
// budget. Use the same Idempotency-Key as the MintPayment call.
func (c *Client) ReportUsage(ctx context.Context, in ReportUsageInput) (*UsageReportResult, error) {
	body := map[string]any{
		"payment_id":        in.PaymentID,
		"actual_work_units": in.ActualWorkUnits,
	}
	var out UsageReportResult
	if err := c.do(ctx, http.MethodPost, "/v1/usage/report", body, in.IdempotencyKey, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// ---- internals ----

func (c *Client) do(
	ctx context.Context,
	method, path string,
	body any,
	idempotencyKey string,
	out any,
) error {
	var reader io.Reader
	if body != nil {
		buf, err := json.Marshal(body)
		if err != nil {
			return fmt.Errorf("open-clearinghouse: marshal request body: %w", err)
		}
		reader = bytes.NewReader(buf)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, reader)
	if err != nil {
		return fmt.Errorf("open-clearinghouse: build request: %w", err)
	}
	req.Header.Set("X-API-Key", c.apiKey)
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if idempotencyKey != "" {
		req.Header.Set("Idempotency-Key", idempotencyKey)
	}

	res, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("open-clearinghouse: do: %w", err)
	}
	defer func() { _ = res.Body.Close() }()
	payload, err := io.ReadAll(res.Body)
	if err != nil {
		return fmt.Errorf("open-clearinghouse: read body: %w", err)
	}
	if res.StatusCode >= 200 && res.StatusCode < 300 {
		if out == nil || len(payload) == 0 {
			return nil
		}
		if err := json.Unmarshal(payload, out); err != nil {
			return fmt.Errorf("open-clearinghouse: decode response: %w", err)
		}
		return nil
	}
	return parseError(res.StatusCode, res.Header.Get("Retry-After"), payload)
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

// newUUIDv4 generates a v4 UUID without pulling in google/uuid. Caller
// can override SubmitJobInput.RequestID with their own.
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
