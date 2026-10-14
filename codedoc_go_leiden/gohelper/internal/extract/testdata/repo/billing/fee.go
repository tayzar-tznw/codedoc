// Package billing computes fees.
package billing

// FeeCalculator computes fees.
type FeeCalculator struct {
	Base
	rate int
}

type Base struct{}

// Calculate returns the fee.
func (c *FeeCalculator) Calculate(p int) int {
	return round(p * c.rate)
}

func round(v int) int { return v }
