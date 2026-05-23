// Package pymthouse is a reference Go SDK for the PymtHouse payment
// clearinghouse. It wraps the few HTTP endpoints app developers need:
// discovery, payment mint, and usage reconciliation.
//
// Construct one Client per process and reuse it. The zero value is not
// useful — always go through NewClient.
package pymthouse

import (
	"bytes"
	"context"
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
			return fmt.Errorf("pymthouse: marshal request body: %w", err)
		}
		reader = bytes.NewReader(buf)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, reader)
	if err != nil {
		return fmt.Errorf("pymthouse: build request: %w", err)
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
		return fmt.Errorf("pymthouse: do: %w", err)
	}
	defer func() { _ = res.Body.Close() }()
	payload, err := io.ReadAll(res.Body)
	if err != nil {
		return fmt.Errorf("pymthouse: read body: %w", err)
	}
	if res.StatusCode >= 200 && res.StatusCode < 300 {
		if out == nil || len(payload) == 0 {
			return nil
		}
		if err := json.Unmarshal(payload, out); err != nil {
			return fmt.Errorf("pymthouse: decode response: %w", err)
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
