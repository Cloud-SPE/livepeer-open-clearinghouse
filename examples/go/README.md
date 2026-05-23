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

    mint, err := ph.MintPayment(ctx, openclearinghouse.MintPaymentInput{
        Capability:     "openai:chat-completions",
        Offering:       "vllm-qwen3.6-27b-default",
        WorkUnits:      1000,
        IdempotencyKey: idem,
    })
    if err != nil {
        var phErr *openclearinghouse.Error
        if errors.As(err, &phErr) && phErr.IsInsufficientCredit() {
            log.Println("need topup:", phErr.Details)
        }
        return err
    }

    // ... POST to mint.RecipientEthAddress's orch with header
    //     Livepeer-Payment: mint.PaymentBytes ...

    _, err = ph.ReportUsage(ctx, openclearinghouse.ReportUsageInput{
        PaymentID:       mint.PaymentID,
        ActualWorkUnits: 873,
        IdempotencyKey:  idem,
    })
    return err
}
```

Method surface:

| | |
|---|---|
| `ListCapabilities(ctx)` | discovery |
| `ListOrchestrators(ctx, capability)` | discovery |
| `MintPayment(ctx, MintPaymentInput)` | the load-bearing call |
| `ReportUsage(ctx, ReportUsageInput)` | reconcile over-committed budget |

Errors come back as `*openclearinghouse.Error` with predicate methods:
`IsInsufficientCredit`, `IsSpendCapExceeded`, `IsAccountNotApproved`,
`IsEmailNotVerified`, `IsNoRouteAvailable`, `IsRateLimited` (with
`RetryAfterSeconds`), `IsDuplicateRequest`, `IsDaemonUnavailable`.
Use `errors.As(err, &phErr)` to access them.
