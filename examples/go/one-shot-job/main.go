// End-to-end example: submit a job via the handoff-mode Go SDK.
//
//	OPEN_CLEARINGHOUSE_URL=http://localhost:8000 \
//	OPEN_CLEARINGHOUSE_API_KEY=pymth_live_... \
//	OPEN_CLEARINGHOUSE_OFFERING=gpt-oss-20b \
//	OPEN_CLEARINGHOUSE_MODEL=gpt-oss-20b \
//	go run ./examples/go/one-shot-job
//
// The SDK handles the handoff dance: opens a job via POST /v1/jobs
// for a route-locked spend authorization, calls the broker directly with the
// authorization and caller proof, reads Livepeer-Work-Units from the
// broker's response, and posts settle back to LOC.
//
// OPEN_CLEARINGHOUSE_OFFERING is optional and defaults to gpt-oss-20b.
// OPEN_CLEARINGHOUSE_MODEL is the OpenAI model name the offering advertises
// (its extra.openai.model); it defaults to the offering id.
// The caller proof is signed by an ephemeral secp256k1 key generated for
// this run (see callerKey below).
package main

import (
	"context"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
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

	client, err := loc.NewClient(loc.Options{
		BaseURL: baseURL,
		APIKey:  apiKey,
	})
	if err != nil {
		return err
	}

	offering := os.Getenv("OPEN_CLEARINGHOUSE_OFFERING")
	if offering == "" {
		offering = "gpt-oss-20b"
	}
	model := os.Getenv("OPEN_CLEARINGHOUSE_MODEL")
	if model == "" {
		model = offering
	}
	body, err := json.Marshal(map[string]any{
		"model":      model,
		"messages":   []map[string]string{{"role": "user", "content": "explain handoff mode"}},
		"max_tokens": 500,
	})
	if err != nil {
		return err
	}

	caller, err := newCallerKey()
	if err != nil {
		return err
	}

	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	result, err := client.SubmitJob(ctx, loc.SubmitJobInput{
		Capability:      "openai:chat-completions",
		Offering:        offering,
		EstimatedUnits:  200,
		MaxTotalUnits:   2000,
		CallerPublicKey: caller.PublicKeyHex(),
		SignCallerProof: caller.Sign,
		Body:            body,
	})
	if err != nil {
		var apiErr *loc.Error
		if errors.As(err, &apiErr) {
			fmt.Printf("loc error: %s - %s\n", apiErr.Code, apiErr.Message)
			return nil
		}
		return err
	}

	if result.Status == 200 {
		fmt.Println("==== broker response ====")
		if result.Body != nil {
			fmt.Println(string(result.Body))
		} else {
			fmt.Println(result.BodyText)
		}
		fmt.Println()
		fmt.Println("==== final accounting ====")
		fmt.Printf("actual units consumed: %d\n", result.ActualUnits)
		fmt.Printf("billed:                %s wei\n", result.BilledValueWei)
		fmt.Printf("refund:                %s wei\n", result.RefundWei)
		fmt.Printf("outcome:               %s\n", result.Outcome)
		if result.CapStatus.WillRefuseNextRefill {
			reason := "unknown"
			if result.CapStatus.WinddownReason != nil {
				reason = *result.CapStatus.WinddownReason
			}
			fmt.Printf("⚠️  cap warning: %s — another job at this size may be refused\n", reason)
		}
	} else {
		fmt.Printf("broker returned %d\n", result.Status)
		fmt.Println(result.BodyText)
	}
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
