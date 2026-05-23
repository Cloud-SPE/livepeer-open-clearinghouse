// End-to-end example: mint a payment, simulate sending to an orch, reconcile usage.
//
//	OPEN_CLEARINGHOUSE_URL=http://localhost:8000 \
//	OPEN_CLEARINGHOUSE_API_KEY=pymth_live_... \
//	go run ./cmd/example
package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"log"
	"os"
	"time"

	openclearinghouse "github.com/livepeer/livepeer-open-clearinghouse-sdk-go/livepeer_open_clearinghouse"
)

func main() {
	if err := run(); err != nil {
		log.Fatal(err)
	}
}

func run() error {
	baseURL := mustEnv("OPEN_CLEARINGHOUSE_URL")
	apiKey := mustEnv("OPEN_CLEARINGHOUSE_API_KEY")
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	ph, err := openclearinghouse.NewClient(openclearinghouse.Options{BaseURL: baseURL, APIKey: apiKey})
	if err != nil {
		return err
	}

	// 1. Pick an offering
	caps, err := ph.ListCapabilities(ctx)
	if err != nil {
		return fmt.Errorf("list capabilities: %w", err)
	}
	var offering string
	for _, c := range caps {
		if c.Name == "openai:chat-completions" && len(c.Offerings) > 0 {
			offering = c.Offerings[0].ID
			break
		}
	}
	if offering == "" {
		return errors.New("no chat-completions offering advertised right now")
	}
	fmt.Println("using offering:", offering)

	// 2. Mint with a 1000-token budget; one Idempotency-Key per logical request
	idem := newIdempotencyKey()
	mint, err := ph.MintPayment(ctx, openclearinghouse.MintPaymentInput{
		Capability:     "openai:chat-completions",
		Offering:       offering,
		WorkUnits:      1000,
		IdempotencyKey: idem,
	})
	if err != nil {
		var phErr *openclearinghouse.Error
		if errors.As(err, &phErr) {
			switch {
			case phErr.IsInsufficientCredit():
				fmt.Println("need topup:", phErr.Details)
				return nil
			case phErr.IsNoRouteAvailable():
				fmt.Println("no orch advertising this offering — try another")
				return nil
			case phErr.IsRateLimited():
				fmt.Printf("rate limited; retry in %ds\n", phErr.RetryAfterSeconds)
				return nil
			}
		}
		return err
	}
	fmt.Printf("minted: work_id=%s… ev=%s\n", mint.WorkID[:min(16, len(mint.WorkID))], mint.ExpectedValueWei)
	fmt.Println("orch:", mint.RecipientEthAddress)
	fmt.Println("Livepeer-Payment header (truncated):", mint.PaymentBytes[:min(48, len(mint.PaymentBytes))]+"…")

	// 3. Real code POSTs to the orch's URL here. Pretend it consumed 873 tokens.
	const actualTokens = 873

	// 4. Reconcile
	result, err := ph.ReportUsage(ctx, openclearinghouse.ReportUsageInput{
		PaymentID:       mint.PaymentID,
		ActualWorkUnits: actualTokens,
		IdempotencyKey:  idem,
	})
	if err != nil {
		return fmt.Errorf("report usage: %w", err)
	}
	fmt.Printf("refunded %s wei; new balance %s wei\n", result.RefundedWei, result.NewBalanceWei)
	return nil
}

func mustEnv(name string) string {
	v := os.Getenv(name)
	if v == "" {
		log.Fatalf("missing required env var: %s", name)
	}
	return v
}

func newIdempotencyKey() string {
	buf := make([]byte, 16)
	_, _ = rand.Read(buf)
	return hex.EncodeToString(buf)
}
