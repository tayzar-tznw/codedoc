package billing

import "testing"

func TestCalculate(t *testing.T) {
	c := NewFeeCalculator(10)
	if got := c.Calculate(10, 3); got != 30 {
		t.Fatalf("Calculate = %d", got)
	}
	if got := Round(1234); got != 1230 {
		t.Fatalf("Round = %d", got)
	}
}
