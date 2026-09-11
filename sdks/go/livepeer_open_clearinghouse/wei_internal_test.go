package openclearinghouse

import (
	"encoding/json"
	"testing"
)

func TestWeiFromAnyAndTelemetry(t *testing.T) {
	cases := []struct {
		in   any
		want string
		ok   bool
	}{
		{"12345678901234567890", "12345678901234567890", true},
		{" 42 ", "42", true},
		{float64(100000), "100000", true},
		{float64(1.5), "", false},
		{json.Number("77"), "77", true},
		{nil, "", false},
		{"", "", false},
		{"abc", "", false},
		{true, "", false},
		{NewWei(3), "3", true},
		{Wei{}, "", false},
		{int64(9), "9", true},
		{int(8), "8", true},
	}
	for _, c := range cases {
		got, ok := weiFromAny(c.in)
		if ok != c.ok || (ok && got.String() != c.want) {
			t.Errorf("weiFromAny(%#v) = (%s, %v), want (%s, %v)", c.in, got, ok, c.want, c.ok)
		}
	}
	if v := weiTelemetryAny("5"); v != "5" {
		t.Errorf("weiTelemetryAny: %#v", v)
	}
	if v := weiTelemetryAny(nil); v != nil {
		t.Errorf("weiTelemetryAny(nil): %#v", v)
	}
	if v := weiTelemetry(Wei{}); v != nil {
		t.Errorf("weiTelemetry(nil): %#v", v)
	}
	if v := weiTelemetry(NewWei(6)); v != "6" {
		t.Errorf("weiTelemetry: %#v", v)
	}
	// refillEvent reads strings (current wire) and legacy floats alike.
	ev := refillEvent(map[string]any{"refill_seq": float64(2), "expected_value_wei": "12345678901234567890", "funded_value_wei": float64(50000)})
	if ev.RefillSeq == nil || *ev.RefillSeq != 2 || ev.ExpectedValueWei.String() != "12345678901234567890" || ev.FundedValueWei.String() != "50000" {
		t.Errorf("refillEvent: %+v", ev)
	}
	if !refillEvent(map[string]any{}).FundedValueWei.IsNil() {
		t.Error("absent funded_value_wei should be nil")
	}
}
