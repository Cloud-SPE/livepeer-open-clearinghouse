// SDK-side telemetry emitter (Go SDK).
//
// Parity with the Python reference at
// `examples/python/src/livepeer_open_clearinghouse_sdk/telemetry.py`:
// fire-and-forget, batched, flush-on-critical, bounded buffer with
// oldest-dropped + log on overflow, gzip > 1 KiB, 3-attempt
// exponential backoff. Telemetry is mandatory — no "telemetry=false"
// option; customers route to operator-side ingest filtering for
// allow/quiet behavior.

package openclearinghouse

import (
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"strings"
	"sync"
	"time"
)

// Defaults mirror exec-plan 002 §"Mechanism".
const (
	telemetryDefaultBatchSize          = 100
	telemetryDefaultFlushInterval      = 5 * time.Second
	telemetryDefaultBufferCap          = 10_000
	telemetryDefaultRetries            = 3
	telemetryDefaultGzipThresholdBytes = 1024
)

// criticalEventTypes are flushed immediately (alongside any *.error
// suffix).
var criticalEventTypes = map[string]struct{}{
	"session.refill_denied": {},
	"session.closed":        {},
}

func isCriticalTelemetryEvent(eventType string) bool {
	if _, ok := criticalEventTypes[eventType]; ok {
		return true
	}
	return strings.HasSuffix(eventType, ".error")
}

type telemetryEvent struct {
	EventType          string                 `json:"event_type"`
	EventSchemaVersion int                    `json:"event_schema_version"`
	CorrelationID      *string                `json:"correlation_id"`
	ClientTS           string                 `json:"client_ts"`
	Payload            map[string]interface{} `json:"payload"`
}

// TelemetryEmitter owns the SDK-side telemetry buffer + flush loop.
// Construct via Client; do not instantiate directly. Close() drains
// the remainder with one final best-effort flush.
type TelemetryEmitter struct {
	http        *http.Client
	endpoint    string
	apiKey      string
	sdkIdentity string

	batchSize          int
	flushInterval      time.Duration
	bufferCap          int
	maxRetries         int
	gzipThresholdBytes int

	mu        sync.Mutex
	buffer    []telemetryEvent
	dropped   int
	closed    bool
	flushChan chan struct{}
	doneChan  chan struct{}
}

// TelemetryEmitterOptions tunes the buffer + flush behavior. All
// fields are optional; zero values fall back to the documented
// defaults.
type TelemetryEmitterOptions struct {
	HTTP               *http.Client
	BaseURL            string
	APIKey             string
	SDKIdentity        string
	Endpoint           string
	BatchSize          int
	FlushInterval      time.Duration
	BufferCap          int
	MaxRetries         int
	GzipThresholdBytes int
}

func newTelemetryEmitter(opts TelemetryEmitterOptions) *TelemetryEmitter {
	if opts.Endpoint == "" {
		opts.Endpoint = "/v1/telemetry"
	}
	if opts.BatchSize == 0 {
		opts.BatchSize = telemetryDefaultBatchSize
	}
	if opts.FlushInterval == 0 {
		opts.FlushInterval = telemetryDefaultFlushInterval
	}
	if opts.BufferCap == 0 {
		opts.BufferCap = telemetryDefaultBufferCap
	}
	if opts.MaxRetries == 0 {
		opts.MaxRetries = telemetryDefaultRetries
	}
	if opts.GzipThresholdBytes == 0 {
		opts.GzipThresholdBytes = telemetryDefaultGzipThresholdBytes
	}
	em := &TelemetryEmitter{
		http:               opts.HTTP,
		endpoint:           strings.TrimRight(opts.BaseURL, "/") + opts.Endpoint,
		apiKey:             opts.APIKey,
		sdkIdentity:        opts.SDKIdentity,
		batchSize:          opts.BatchSize,
		flushInterval:      opts.FlushInterval,
		bufferCap:          opts.BufferCap,
		maxRetries:         opts.MaxRetries,
		gzipThresholdBytes: opts.GzipThresholdBytes,
		buffer:             make([]telemetryEvent, 0, opts.BufferCap/4),
		flushChan:          make(chan struct{}, 1),
		doneChan:           make(chan struct{}),
	}
	go em.loop()
	return em
}

// uuidNamespaceURL is RFC 4122's NAMESPACE_URL
// (6ba7b811-9dad-11d1-80b4-00c04fd430c8).
var uuidNamespaceURL = [16]byte{
	0x6b, 0xa7, 0xb8, 0x11, 0x9d, 0xad, 0x11, 0xd1,
	0x80, 0xb4, 0x00, 0xc0, 0x4f, 0xd4, 0x30, 0xc8,
}

// TelemetryCorrelationID maps an SDK correlation value (request id,
// session id, ...) to the UUID the gateway's /v1/telemetry schema
// requires. A value that already is a UUID (8-4-4-4-12 hex, any
// version) is returned lowercased; anything else becomes the RFC 4122
// v5 UUID of the string under NAMESPACE_URL, so every SDK derives the
// same id for the same request id.
func TelemetryCorrelationID(value string) string {
	if isUUIDString(value) {
		return strings.ToLower(value)
	}
	return uuidV5(uuidNamespaceURL, value)
}

func isUUIDString(s string) bool {
	if len(s) != 36 {
		return false
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch i {
		case 8, 13, 18, 23:
			if c != '-' {
				return false
			}
		default:
			isHex := (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F')
			if !isHex {
				return false
			}
		}
	}
	return true
}

func uuidV5(namespace [16]byte, name string) string {
	h := sha1.New()
	_, _ = h.Write(namespace[:])
	_, _ = h.Write([]byte(name))
	sum := h.Sum(nil)
	var b [16]byte
	copy(b[:], sum[:16])
	b[6] = (b[6] & 0x0f) | 0x50 // version 5
	b[8] = (b[8] & 0x3f) | 0x80 // variant RFC 4122
	hexed := hex.EncodeToString(b[:])
	return hexed[0:8] + "-" + hexed[8:12] + "-" + hexed[12:16] + "-" + hexed[16:20] + "-" + hexed[20:32]
}

// EmitTelemetryOptions is the public per-event input.
type EmitTelemetryOptions struct {
	EventType          string
	EventSchemaVersion int
	CorrelationID      string // empty string ↦ null on the wire
	Payload            map[string]interface{}
	ClientTS           string // empty string ↦ now()
}

// Emit appends one event to the buffer. Never blocks more than the
// time it takes to acquire the internal mutex; never returns an
// error.
func (e *TelemetryEmitter) Emit(opts EmitTelemetryOptions) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.closed {
		return
	}
	var correlationID *string
	if opts.CorrelationID != "" {
		cid := TelemetryCorrelationID(opts.CorrelationID)
		correlationID = &cid
	}
	clientTS := opts.ClientTS
	if clientTS == "" {
		clientTS = time.Now().UTC().Format(time.RFC3339Nano)
	}
	if opts.EventSchemaVersion == 0 {
		opts.EventSchemaVersion = 1
	}
	if opts.Payload == nil {
		// The gateway schema requires an object; a nil map would encode
		// as JSON null and the whole batch would be rejected (422).
		opts.Payload = map[string]interface{}{}
	}
	if len(e.buffer) == e.bufferCap {
		e.buffer = e.buffer[1:]
		e.dropped++
		log.Printf("[telemetry] buffer full; dropped oldest event (total dropped=%d)", e.dropped)
	}
	e.buffer = append(e.buffer, telemetryEvent{
		EventType:          opts.EventType,
		EventSchemaVersion: opts.EventSchemaVersion,
		CorrelationID:      correlationID,
		ClientTS:           clientTS,
		Payload:            opts.Payload,
	})
	if isCriticalTelemetryEvent(opts.EventType) || len(e.buffer) >= e.batchSize {
		select {
		case e.flushChan <- struct{}{}:
		default:
		}
	}
}

// BufferSize is the current count of unflushed events. Useful for tests.
func (e *TelemetryEmitter) BufferSize() int {
	e.mu.Lock()
	defer e.mu.Unlock()
	return len(e.buffer)
}

// Dropped is the count of events lost to buffer overflow.
func (e *TelemetryEmitter) Dropped() int {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.dropped
}

// Close stops the flush loop after one last drain. Idempotent.
func (e *TelemetryEmitter) Close(ctx context.Context) {
	e.mu.Lock()
	if e.closed {
		e.mu.Unlock()
		return
	}
	e.closed = true
	e.mu.Unlock()

	// Signal the loop to drain + exit.
	select {
	case e.flushChan <- struct{}{}:
	default:
	}
	close(e.flushChan)

	select {
	case <-e.doneChan:
	case <-ctx.Done():
	}
}

func (e *TelemetryEmitter) loop() {
	defer close(e.doneChan)
	ticker := time.NewTicker(e.flushInterval)
	defer ticker.Stop()
	for {
		select {
		case _, ok := <-e.flushChan:
			e.flushOnce()
			if !ok {
				// Channel closed — Close() called.
				return
			}
		case <-ticker.C:
			e.flushOnce()
		}
	}
}

func (e *TelemetryEmitter) flushOnce() {
	e.mu.Lock()
	if len(e.buffer) == 0 {
		e.mu.Unlock()
		return
	}
	batch := e.buffer
	e.buffer = make([]telemetryEvent, 0, e.bufferCap/4)
	e.mu.Unlock()

	body, err := json.Marshal(map[string]interface{}{"events": batch})
	if err != nil {
		log.Printf("[telemetry] marshal failed: %v", err)
		return
	}
	headers := map[string]string{
		"Content-Type":                    "application/json",
		"X-API-Key":                       e.apiKey,
		"Livepeer-Open-Clearinghouse-SDK": e.sdkIdentity,
	}
	if len(body) > e.gzipThresholdBytes {
		gzipped, gerr := gzipBytes(body)
		if gerr == nil {
			body = gzipped
			headers["Content-Encoding"] = "gzip"
		}
	}
	e.sendWithRetry(body, headers, len(batch))
}

func (e *TelemetryEmitter) sendWithRetry(body []byte, headers map[string]string, count int) {
	backoff := 500 * time.Millisecond
	for attempt := 1; attempt <= e.maxRetries; attempt++ {
		req, err := http.NewRequest(http.MethodPost, e.endpoint, bytes.NewReader(body))
		if err == nil {
			for k, v := range headers {
				req.Header.Set(k, v)
			}
			resp, doErr := e.http.Do(req)
			if doErr == nil {
				_ = resp.Body.Close()
				if resp.StatusCode < 500 && resp.StatusCode != http.StatusTooManyRequests {
					return
				}
			}
		}
		if attempt < e.maxRetries {
			time.Sleep(backoff)
			backoff *= 2
		}
	}
	log.Printf("[telemetry] flush dropped %d events after retries", count)
}

func gzipBytes(in []byte) ([]byte, error) {
	var buf bytes.Buffer
	w := gzip.NewWriter(&buf)
	if _, err := w.Write(in); err != nil {
		return nil, err
	}
	if err := w.Close(); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

// readAll is a small helper to keep an import out of the call site
// (some Go versions of net/http need explicit body draining).
var _ = io.ReadAll
