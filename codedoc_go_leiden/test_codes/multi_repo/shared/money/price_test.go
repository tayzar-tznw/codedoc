package money

import "testing"

func TestFormat(t *testing.T) {
	if got := NewPrice(1234).Format(); got != "1230 円" {
		t.Fatalf("Format() = %q", got)
	}
}
