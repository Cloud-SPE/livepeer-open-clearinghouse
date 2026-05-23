# pymthouse-sdk-go

Reference Go SDK for the PymtHouse gateway. Standard-library only —
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
go test ./...
```

Uses `httptest.Server` to stub PymtHouse's HTTP surface; no live
gateway needed.

## Run the example against a live stack

```bash
PYMTHOUSE_URL=http://localhost:8000 \
PYMTHOUSE_API_KEY=pymth_live_xxxxxxxx_xxxxxxxxxxxxxxxxxxxxxxxx \
go run ./cmd/example
```

## Use it from your app

```go
import (
    "context"
    "errors"
    "log"
    "os"

    "github.com/livepeer/pymthouse-sdk-go/pymthouse"
)

func callLLM(ctx context.Context, prompt string) error {
    ph, err := pymthouse.NewClient(pymthouse.Options{
        BaseURL: "https://pymthouse.example.com",
        APIKey:  os.Getenv("PYMTHOUSE_API_KEY"),
    })
    if err != nil {
        return err
    }

    mint, err := ph.MintPayment(ctx, pymthouse.MintPaymentInput{
        Capability:     "openai:chat-completions",
        Offering:       "vllm-qwen3.6-27b-default",
        WorkUnits:      1000,
        IdempotencyKey: idem,
    })
    if err != nil {
        var phErr *pymthouse.Error
        if errors.As(err, &phErr) && phErr.IsInsufficientCredit() {
            log.Println("need topup:", phErr.Details)
        }
        return err
    }

    // ... POST to mint.RecipientEthAddress's orch with header
    //     Livepeer-Payment: mint.PaymentBytes ...

    _, err = ph.ReportUsage(ctx, pymthouse.ReportUsageInput{
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

Errors come back as `*pymthouse.Error` with predicate methods:
`IsInsufficientCredit`, `IsSpendCapExceeded`, `IsAccountNotApproved`,
`IsEmailNotVerified`, `IsNoRouteAvailable`, `IsRateLimited` (with
`RetryAfterSeconds`), `IsDuplicateRequest`, `IsDaemonUnavailable`.
Use `errors.As(err, &phErr)` to access them.
