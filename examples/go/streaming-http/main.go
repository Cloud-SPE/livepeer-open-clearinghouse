// Streaming session with HTTP topup (live-session-remote-runner@v0).
//
//	OPEN_CLEARINGHOUSE_URL=http://localhost:8000 \
//	OPEN_CLEARINGHOUSE_API_KEY=pymth_live_... \
//	go run ./examples/go/streaming-http
//
// For HTTP-topup modes, the broker doesn't push balance-low frames over
// a WebSocket — the customer's media plane observes balance-low
// out-of-band and routes the signal in via runner.OnBalanceLow(). The
// runner then asks LOC for a refill and POSTs it to the broker's
// control.topup_url.
package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"os"
	"time"

	loc "github.com/livepeer/livepeer-open-clearinghouse-sdk-go/livepeer_open_clearinghouse"
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

	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	handle, err := client.OpenSession(ctx, loc.OpenSessionInput{
		Capability:           "livepeer:remote-runner",
		Offering:             "live-session-remote-runner",
		EstimatedRunwayUnits: 1000,
		MaxTotalUnits:        10000,
	})
	if err != nil {
		var apiErr *loc.Error
		if errors.As(err, &apiErr) {
			fmt.Printf("loc error: %s - %s\n", apiErr.Code, apiErr.Message)
			return nil
		}
		return err
	}
	fmt.Printf("session opened: %s (mode=%s)\n", handle.SessionID, handle.Mode)

	runner := loc.NewSessionRunner(loc.SessionRunnerOptions{
		Client: client,
		Handle: handle,
		OnRefillSucceeded: func(e loc.RefillEvent) {
			seq := "?"
			if e.RefillSeq != nil {
				seq = fmt.Sprintf("%d", *e.RefillSeq)
			}
			funded := int64(0)
			if e.FundedValueWei != nil {
				funded = *e.FundedValueWei
			}
			fmt.Printf("refill #%s: +%d wei\n", seq, funded)
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
	observed := int64(500)
	runner.OnBalanceLow(ctx, &observed, "")

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
