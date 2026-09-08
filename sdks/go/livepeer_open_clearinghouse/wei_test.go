package openclearinghouse_test

import (
	"context"
	"encoding/json"
	"math"
	"math/big"
	"net/http"
	"net/http/httptest"
	"testing"

	loc "github.com/livepeer/livepeer-open-clearinghouse-sdk-go/livepeer_open_clearinghouse"
)

const hugeWei = "12345678901234567890" // > math.MaxInt64

func TestWeiRoundTripsBeyondInt64(t *testing.T) {
	var w loc.Wei
	if err := json.Unmarshal([]byte(`"`+hugeWei+`"`), &w); err != nil {
		t.Fatal(err)
	}
	if w.String() != hugeWei {
		t.Fatalf("String: got %q, want %q", w.String(), hugeWei)
	}
	want, _ := new(big.Int).SetString(hugeWei, 10)
	if w.Cmp(loc.WeiFromBig(want)) != 0 {
		t.Fatalf("Cmp: %s != %s", w, want)
	}
	if v, ok := w.Int64(); ok || v != 0 {
		t.Fatalf("Int64 should report overflow, got (%d, %v)", v, ok)
	}
	out, err := json.Marshal(w)
	if err != nil {
		t.Fatal(err)
	}
	if string(out) != `"`+hugeWei+`"` {
		t.Fatalf("Marshal: got %s", out)
	}
	// Whole-struct round trip through a public wire type.
	var status loc.JobStatusResponse
	if err := json.Unmarshal([]byte(`{"funded_value_wei":"`+hugeWei+`","billed_value_wei":null}`), &status); err != nil {
		t.Fatal(err)
	}
	if status.FundedValueWei.String() != hugeWei || !status.BilledValueWei.IsNil() {
		t.Fatalf("struct decode: funded=%s billed=%v", status.FundedValueWei, status.BilledValueWei)
	}
	encoded, _ := json.Marshal(status)
	var back loc.JobStatusResponse
	if err := json.Unmarshal(encoded, &back); err != nil {
		t.Fatal(err)
	}
	if back.FundedValueWei.Cmp(status.FundedValueWei) != 0 || !back.BilledValueWei.IsNil() {
		t.Fatalf("struct round trip: %s", encoded)
	}
}

func TestWeiAcceptsLegacyBareNumber(t *testing.T) {
	var w loc.Wei
	if err := json.Unmarshal([]byte(`100000`), &w); err != nil {
		t.Fatal(err)
	}
	if v, ok := w.Int64(); !ok || v != 100000 {
		t.Fatalf("legacy number: got (%d, %v)", v, ok)
	}
	if err := json.Unmarshal([]byte(`-5`), &w); err != nil {
		t.Fatal(err)
	}
	if w.String() != "-5" {
		t.Fatalf("negative: got %s", w)
	}
	for _, bad := range []string{`1.5`, `1e5`, `"abc"`, `""`, `true`, `"1.0"`} {
		var x loc.Wei
		if err := json.Unmarshal([]byte(bad), &x); err == nil {
			t.Errorf("expected %s to be rejected", bad)
		}
	}
}

func TestWeiNullAndZeroValue(t *testing.T) {
	var w loc.Wei
	if err := json.Unmarshal([]byte(`null`), &w); err != nil {
		t.Fatal(err)
	}
	if !w.IsNil() || w.String() != "0" {
		t.Fatalf("null: nil=%v str=%q", w.IsNil(), w.String())
	}
	if _, ok := w.Int64(); ok {
		t.Fatal("nil Int64 should report false")
	}
	if w.Cmp(loc.Wei{}) != 0 || w.Cmp(loc.NewWei(1)) != -1 || loc.NewWei(1).Cmp(w) != 1 {
		t.Fatal("nil should compare as zero")
	}
	out, _ := json.Marshal(struct {
		A loc.Wei `json:"a"`
		B loc.Wei `json:"b"`
	}{B: loc.NewWei(7)})
	if string(out) != `{"a":null,"b":"7"}` {
		t.Fatalf("marshal: %s", out)
	}
	if _, err := loc.ParseWei("12x"); err == nil {
		t.Fatal("ParseWei should reject non-digits")
	}
	if p, err := loc.ParseWei(hugeWei); err != nil || p.String() != hugeWei {
		t.Fatalf("ParseWei: %v %s", err, p)
	}
	if v, ok := loc.NewWei(math.MaxInt64).Int64(); !ok || v != math.MaxInt64 {
		t.Fatal("MaxInt64 should fit")
	}
	if !loc.WeiFromBig(nil).IsNil() {
		t.Fatal("WeiFromBig(nil) should be nil")
	}
}

func TestGetJobStatusParsesHugeWei(t *testing.T) {
	loca := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"job_id":"j","request_id":"r","work_id":"w","state":"closed",
			"accounting_outcome":"exact","broker_exchange_outcome":null,"actual_units":1,
			"billed_value_wei":"` + hugeWei + `","funded_value_wei":"` + hugeWei + `1",
			"opened_at":"2026-05-24T12:00:00Z","closed_at":null}`))
	}))
	defer loca.Close()
	client, _ := loc.NewClient(loc.Options{BaseURL: loca.URL, APIKey: apiKey})
	status, err := client.GetJobStatus(context.Background(), "j")
	if err != nil {
		t.Fatal(err)
	}
	if status.BilledValueWei.String() != hugeWei || status.FundedValueWei.String() != hugeWei+"1" {
		t.Fatalf("got billed=%s funded=%s", status.BilledValueWei, status.FundedValueWei)
	}
}
