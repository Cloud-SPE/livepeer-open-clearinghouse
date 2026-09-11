package openclearinghouse

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math/big"
	"strconv"
	"strings"
)

// Wei is an exact wei amount as carried by LOC job and session
// responses. On the wire every `*_wei` field is a decimal integer
// STRING (never a JSON number) and may exceed int64, so the SDK keeps
// the value as a big.Int rather than truncating it.
//
// Wei embeds *big.Int; a nil Int means the field was null or absent.
// Compare with Cmp (== compares pointers), read the digits with String,
// and use Int64 when you need a machine word and can tolerate the
// overflow flag.
type Wei struct{ *big.Int }

// NewWei wraps an int64 as a Wei.
func NewWei(v int64) Wei { return Wei{big.NewInt(v)} }

// WeiFromBig wraps an existing *big.Int (copied) as a Wei. A nil input
// yields the nil Wei.
func WeiFromBig(v *big.Int) Wei {
	if v == nil {
		return Wei{}
	}
	return Wei{new(big.Int).Set(v)}
}

// ParseWei parses a decimal integer string (optionally negative) into a
// Wei. It rejects anything that is not a plain base-10 integer.
func ParseWei(s string) (Wei, error) {
	w, ok := parseWeiDigits(s)
	if !ok {
		return Wei{}, fmt.Errorf("openclearinghouse: invalid wei amount %q", s)
	}
	return w, nil
}

func parseWeiDigits(s string) (Wei, bool) {
	if s == "" || s == "-" || s == "+" {
		return Wei{}, false
	}
	digits := s
	if digits[0] == '-' || digits[0] == '+' {
		digits = digits[1:]
	}
	for i := 0; i < len(digits); i++ {
		if digits[i] < '0' || digits[i] > '9' {
			return Wei{}, false
		}
	}
	v, ok := new(big.Int).SetString(s, 10)
	if !ok {
		return Wei{}, false
	}
	return Wei{v}, true
}

// IsNil reports whether the amount is absent (JSON null / missing).
func (w Wei) IsNil() bool { return w.Int == nil }

// String renders the amount in decimal. A nil Wei renders as "0".
func (w Wei) String() string {
	if w.Int == nil {
		return "0"
	}
	return w.Int.String()
}

// Int64 returns the amount as an int64. The bool is false when the
// amount is nil or does not fit in an int64 (overflow), in which case
// the int64 is 0.
func (w Wei) Int64() (int64, bool) {
	if w.Int == nil || !w.Int.IsInt64() {
		return 0, false
	}
	return w.Int.Int64(), true
}

// Cmp compares two amounts, treating nil as zero. It returns -1, 0 or
// +1 like big.Int.Cmp.
func (w Wei) Cmp(other Wei) int {
	return w.bigOrZero().Cmp(other.bigOrZero())
}

func (w Wei) bigOrZero() *big.Int {
	if w.Int == nil {
		return new(big.Int)
	}
	return w.Int
}

// MarshalJSON emits the amount as a quoted decimal string, or null when
// the amount is nil.
func (w Wei) MarshalJSON() ([]byte, error) {
	if w.Int == nil {
		return []byte("null"), nil
	}
	return []byte(strconv.Quote(w.Int.String())), nil
}

// UnmarshalJSON accepts a quoted decimal integer string (the current
// wire contract), a bare JSON integer (legacy gateways), or null (nil).
// Fractional or exponent-form numbers are rejected: wei is integral.
func (w *Wei) UnmarshalJSON(data []byte) error {
	data = bytes.TrimSpace(data)
	if len(data) == 0 || bytes.Equal(data, []byte("null")) {
		w.Int = nil
		return nil
	}
	var text string
	if data[0] == '"' {
		if err := json.Unmarshal(data, &text); err != nil {
			return fmt.Errorf("openclearinghouse: invalid wei amount %s: %w", data, err)
		}
		text = strings.TrimSpace(text)
	} else {
		text = string(data)
	}
	parsed, ok := parseWeiDigits(text)
	if !ok {
		return fmt.Errorf("openclearinghouse: invalid wei amount %s", data)
	}
	w.Int = parsed.Int
	return nil
}

// weiFromAny converts a decoded JSON scalar (as found in map[string]any
// results) into a Wei. It accepts a string, a float64 (legacy bare
// number, integral only), a json.Number, or an existing Wei. The bool
// is false for nil, missing or unparseable values.
func weiFromAny(v any) (Wei, bool) {
	switch t := v.(type) {
	case nil:
		return Wei{}, false
	case Wei:
		return t, !t.IsNil()
	case *Wei:
		if t == nil || t.IsNil() {
			return Wei{}, false
		}
		return *t, true
	case string:
		return parseWeiDigits(strings.TrimSpace(t))
	case json.Number:
		return parseWeiDigits(t.String())
	case float64:
		if t != float64(int64(t)) {
			return Wei{}, false
		}
		return NewWei(int64(t)), true
	case int64:
		return NewWei(t), true
	case int:
		return NewWei(int64(t)), true
	}
	return Wei{}, false
}

// weiTelemetry renders a wei amount for a telemetry payload: the
// decimal string, or nil when the amount is absent.
func weiTelemetry(w Wei) any {
	if w.IsNil() {
		return nil
	}
	return w.String()
}

// weiTelemetryAny is weiTelemetry for a raw decoded JSON scalar.
func weiTelemetryAny(v any) any {
	w, ok := weiFromAny(v)
	if !ok {
		return nil
	}
	return w.String()
}
