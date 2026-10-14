package clock_test

import (
	"testing"
	"time"

	"example.com/iryo/kernel/clock"
)

func TestExported(t *testing.T) {
	if got := clock.Normalize(" 会計 "); got != "会計" {
		t.Fatalf("Normalize = %q", got)
	}
	if got := clock.NormalizeHook(" 薬局 "); got != "薬局" {
		t.Fatalf("NormalizeHook = %q", got)
	}
}

func TestLabel(t *testing.T) {
	day := time.Date(2026, 10, 13, 0, 0, 0, 0, time.UTC)
	if got := clock.Label(clock.OfficeClock{Day: day}, " 受付 "); got != "受付@2026-10-13" {
		t.Fatalf("Label = %q", got)
	}
}
