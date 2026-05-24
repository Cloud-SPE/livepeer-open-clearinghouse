// End-to-end example: submit a job via the handoff-mode Go SDK.
//
//	OPEN_CLEARINGHOUSE_URL=http://localhost:8000 \
//	OPEN_CLEARINGHOUSE_API_KEY=pymth_live_... \
//	go run ./cmd/example
//
// The SDK handles the handoff dance: opens a job via POST /v1/jobs
// (mints a payment envelope), calls the broker directly with the
// envelope as Livepeer-Payment, reads Livepeer-Work-Units from the
// broker's response, and posts settle back to LOC.
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

	client, err := loc.NewClient(loc.Options{
		BaseURL: baseURL,
		APIKey:  apiKey,
	})
	if err != nil {
		return err
	}

	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	result, err := client.SubmitJob(ctx, loc.SubmitJobInput{
		Capability:     "openai:chat-completions",
		Offering:       "gpt-oss-20b",
		EstimatedUnits: 200,
		MaxTotalUnits:  2000,
		Body:           []byte(`{"messages":[{"role":"user","content":"explain handoff mode"}],"max_tokens":500}`),
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
		fmt.Printf("billed:                %d wei\n", result.BilledValueWei)
		fmt.Printf("refund:                %d wei\n", result.RefundWei)
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
