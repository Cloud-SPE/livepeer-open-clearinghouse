# livepeer-open-clearinghouse-sdk-go

Reference Go SDK for the Livepeer Open Clearinghouse gateway. Standard-library only —
no external dependencies.

## Setup

Go 1.22+ required.

```bash
go mod tidy
```

(There are no dependencies to fetch; this just sanity-checks the
module.)

## Run the tests

```bash
go test ./livepeer_open_clearinghouse/...
```

Uses `httptest.Server` to stub Livepeer Open Clearinghouse's HTTP surface; no live
gateway needed.

## Coverage

```bash
go test ./livepeer_open_clearinghouse/... -coverprofile=cover.out -covermode=atomic
go tool cover -func=cover.out      # text summary
go tool cover -html=cover.out      # opens an HTML report
```

The `cmd/example/` binary is intentionally excluded from coverage —
it's documentation, not library code.

## Lint

```bash
golangci-lint run ./...
gofmt -l .              # list any unformatted files
```

Enabled linters: `errcheck`, `govet`, `staticcheck`, `unused`,
`ineffassign`, `gosec`, `revive`, `gocritic`, `bodyclose`, `misspell`.
Configured in `.golangci.yml`. Install with
`go install github.com/golangci/golangci-lint/v2/cmd/golangci-lint@latest`.

## Run the example against a live stack

```bash
OPEN_CLEARINGHOUSE_URL=http://localhost:8000 \
OPEN_CLEARINGHOUSE_API_KEY=pymth_live_xxxxxxxx_xxxxxxxxxxxxxxxxxxxxxxxx \
go run ./cmd/example
```

## Use it from your app

Livepeer Open Clearinghouse runs in **handoff mode**: LOC mints the
payment envelope; the SDK calls the broker directly with that
envelope; LOC settles based on the broker's reported work units.

```go
import (
    "context"
    "errors"
    "log"
    "os"

    openclearinghouse "github.com/livepeer/livepeer-open-clearinghouse-sdk-go/livepeer_open_clearinghouse"
)

func callLLM(ctx context.Context, prompt string) error {
    ph, err := openclearinghouse.NewClient(openclearinghouse.Options{
        BaseURL: "https://open-clearinghouse.example.com",
        APIKey:  os.Getenv("OPEN_CLEARINGHOUSE_API_KEY"),
    })
    if err != nil {
        return err
    }

    result, err := ph.SubmitJob(ctx, openclearinghouse.SubmitJobInput{
        Capability:     "openai:chat-completions",
        Offering:       "vllm-qwen3.6-27b-default",
        EstimatedUnits: 200,
        MaxTotalUnits:  2000,
        Body: map[string]any{
            "messages":   []any{map[string]any{"role": "user", "content": prompt}},
            "max_tokens": 50,
        },
    })
    if err != nil {
        var phErr *openclearinghouse.Error
        if errors.As(err, &phErr) && phErr.IsInsufficientCredit() {
            log.Println("need topup:", phErr.Details)
        }
        return err
    }
    log.Printf("billed %d wei for %d units, outcome=%s",
        result.BilledValueWei, result.ActualUnits, result.Outcome)
    return nil
}
```

Long-running session shape:

```go
handle, _ := ph.OpenSession(ctx, openclearinghouse.OpenSessionInput{
    Capability:           "cap.live",
    Offering:             "off.live",
    EstimatedRunwayUnits: 1000,
    MaxTotalUnits:        10000,
})
// ... stream work against handle.BrokerURL, refill via SessionRunner ...
_, _ = ph.CloseSession(ctx, openclearinghouse.CloseSessionInput{
    SessionID:   handle.SessionID,
    ActualUnits: 4250,
})
```

Method surface:

| | |
|---|---|
| `ListCapabilities(ctx)` | discovery |
| `ListOrchestrators(ctx, capability)` | discovery |
| `SubmitJob(ctx, SubmitJobInput)` | one-shot job (cases a/b/c) |
| `OpenSession(ctx, OpenSessionInput)` | open long-running session (case d) |
| `RefillSession(ctx, RefillSessionInput)` | top up an open session |
| `CloseSession(ctx, CloseSessionInput)` | settle + close a session |
| `Telemetry()` | direct access to the (mandatory) `*TelemetryEmitter` |

The `Livepeer-Open-Clearinghouse-SDK` identity header is sent on every
call, and telemetry events (`request.mint_started`,
`request.settle_completed`, `session.opened`, …) fire fire-and-forget
through `/v1/telemetry`. There is no telemetry opt-out.

Errors come back as `*openclearinghouse.Error` with predicate methods:
`IsInsufficientCredit`, `IsSpendCapExceeded`, `IsAccountNotApproved`,
`IsEmailNotVerified`, `IsNoRouteAvailable`, `IsRateLimited` (with
`RetryAfterSeconds`), `IsDuplicateRequest`, `IsDaemonUnavailable`.
Use `errors.As(err, &phErr)` to access them.
