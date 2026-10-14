package clock

import (
	"testing"
	"time"
)

type fakeClock struct{}

func (fakeClock) Now() time.Time {
	return time.Date(2000, 1, 2, 0, 0, 0, 0, time.UTC)
}

func TestStamp(t *testing.T) {
	if got := Stamp(fakeClock{}); got != "2000-01-02" {
		t.Fatalf("Stamp = %q", got)
	}
}

func TestNormalize(t *testing.T) {
	if got := normalize("  受付 "); got != "受付" {
		t.Fatalf("normalize = %q", got)
	}
}
