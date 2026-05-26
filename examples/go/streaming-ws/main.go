// Streaming session with WS topup (session-control-plus-media@v0).
//
//	OPEN_CLEARINGHOUSE_URL=http://localhost:8000 \
//	OPEN_CLEARINGHOUSE_API_KEY=pymth_live_... \
//	go run ./examples/go/streaming-ws
//
// SessionRunner connects to the broker over a control WebSocket. When
// the broker pushes a Livepeer-Balance-Low frame, the runner asks LOC
// for a refill and delivers it back as a session.topup frame — the
// OnRefillSucceeded callback fires on each successful top-up.
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
		Capability:           "livepeer:live-video-control",
		Offering:             "session-control-plus-media",
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

	// Hold the session briefly so the broker has a chance to push at
	// least one Livepeer-Balance-Low frame. Production code would drive
	// its own media plane on top of this WS rather than sleeping.
	time.Sleep(3 * time.Second)

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
