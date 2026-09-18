// Extensible paid-session/v1 session with authoritative HTTP top-up.
//
//	OPEN_CLEARINGHOUSE_URL=http://localhost:8000 \
//	OPEN_CLEARINGHOUSE_API_KEY=pymth_live_... \
//	go run ./examples/go/streaming-http
//
// The customer's media plane observes the broker's normative balance
// object and routes it in via runner.OnBalance(). The
// runner then asks LOC for a refill and POSTs it to the broker's
// control.topup_url.
//
// The caller proof is signed by an ephemeral secp256k1 key generated for
// this run (see callerKey below).
package main

import (
	"context"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"log"
	"os"
	"time"

	"github.com/decred/dcrd/dcrec/secp256k1/v4"
	"github.com/decred/dcrd/dcrec/secp256k1/v4/ecdsa"
	loc "github.com/livepeer/livepeer-open-clearinghouse-sdk-go/livepeer_open_clearinghouse"
	"golang.org/x/crypto/sha3"
)

func main() {
	if err := run(); err != nil {
		log.Fatal(err)
	}
}

func run() error {
	baseURL := os.Getenv("OPEN_CLEARINGHOUSE_URL")
	apiKey := os.Getenv("OPEN_CLEARINGHOUSE_API_KEY")
	if baseURL == "" || apiKey == "" {
		return errors.New("set OPEN_CLEARINGHOUSE_URL and OPEN_CLEARINGHOUSE_API_KEY")
	}

	client, err := loc.NewClient(loc.Options{BaseURL: baseURL, APIKey: apiKey})
	if err != nil {
		return err
	}

	caller, err := newCallerKey()
	if err != nil {
		return err
	}

	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	handle, err := client.OpenSession(ctx, loc.OpenSessionInput{
		Capability:           "livepeer:remote-runner",
		Offering:             "live-session-remote-runner",
		DescriptorSchema:     "livepeer.session.remote-runner/v1",
		SessionParams:        map[string]any{},
		EstimatedRunwayUnits: 1000,
		MaxTotalUnits:        10000,
		CallerPublicKey:      caller.PublicKeyHex(),
		SignCallerProof:      caller.Sign,
	})
	if err != nil {
		var apiErr *loc.Error
		if errors.As(err, &apiErr) {
			fmt.Printf("loc error: %s - %s\n", apiErr.Code, apiErr.Message)
			return nil
		}
		return err
	}
	fmt.Printf("session opened: %s (protocol=%s)\n", handle.SessionID, handle.Protocol)

	runner := loc.NewSessionRunner(loc.SessionRunnerOptions{
		Client: client,
		Handle: handle,
		OnRefillSucceeded: func(e loc.RefillEvent) {
			seq := "?"
			if e.RefillSeq != nil {
				seq = fmt.Sprintf("%d", *e.RefillSeq)
			}
			funded := "0"
			if !e.FundedValueWei.IsNil() {
				funded = e.FundedValueWei.String()
			}
			fmt.Printf("refill #%s: +%s wei\n", seq, funded)
		},
		OnRefillRefused: func(e loc.RefillEvent) {
			fmt.Printf("refill refused: %v\n", e.Error)
		},
		OnWinddownWarning: func(w loc.WinddownEvent) {
			fmt.Printf("winddown: %s\n", w.Reason)
		},
	})

	if err := runner.Start(ctx); err != nil {
		return err
	}

	// Customer-driven refill. In production this fires when the media
	// plane observes balance-low on the runner channel.
	runner.OnBalance(ctx, loc.SessionBalance{
		Status: "low", ClaimedUnits: 500, DebitedUnits: 500,
		Unit: "session_second", RunwayUnits: 100,
	})

	settle, err := runner.Close(ctx, loc.CloseSessionInput{
		ActualUnits: 750,
		Outcome:     "complete",
	})
	if err != nil {
		return err
	}
	fmt.Println("==== final settlement ====")
	fmt.Printf("outcome: %v\n", settle["outcome"])
	fmt.Printf("billed:  %v wei\n", settle["billed_value_wei"])
	fmt.Printf("refund:  %v wei\n", settle["refund_wei"])
	return nil
}

// callerKey is the caller identity behind the Livepeer-Caller-Proof. The
// SDK never takes custody of it: it only hands the opaque authorization
// bytes to SignCallerProof. This example generates an ephemeral key per
// run; production callers keep (and protect) their own long-lived key.
type callerKey struct{ priv *secp256k1.PrivateKey }

func newCallerKey() (*callerKey, error) {
	priv, err := secp256k1.GeneratePrivateKey()
	if err != nil {
		return nil, fmt.Errorf("generate caller key: %w", err)
	}
	return &callerKey{priv: priv}, nil
}

// PublicKeyHex is the compressed secp256k1 public key, lowercase hex, no 0x.
func (k *callerKey) PublicKeyHex() string {
	return hex.EncodeToString(k.priv.PubKey().SerializeCompressed())
}

// Sign produces the caller proof: an EIP-191 personal-sign over
// keccak256("livepeer-invocation-proof/v1\x00" || authorization),
// returned as base64(R || S || V) with V = 27 + recovery id.
func (k *callerKey) Sign(authorization []byte) (string, error) {
	digest := keccak256([]byte("livepeer-invocation-proof/v1\x00"), authorization)
	msgHash := keccak256([]byte("\x19Ethereum Signed Message:\n32"), digest)
	compact := ecdsa.SignCompact(k.priv, msgHash, false) // [V, R, S]
	sig := append(compact[1:65:65], compact[0])          // [R, S, V]
	return base64.StdEncoding.EncodeToString(sig), nil
}

func keccak256(parts ...[]byte) []byte {
	h := sha3.NewLegacyKeccak256()
	for _, p := range parts {
		h.Write(p)
	}
	return h.Sum(nil)
}
