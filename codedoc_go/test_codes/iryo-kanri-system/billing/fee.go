package billing

import "example.com/iryo/kernel/guard"

type FeeCalculator struct {
	Rate int
}

func NewFeeCalculator(rate int) *FeeCalculator {
	return &FeeCalculator{Rate: rate}
}

func (c *FeeCalculator) Calculate(points, count int) int {
	return guard.EnsurePositive(points*count) * c.Rate / 10
}

func (c *FeeCalculator) Scaled(factor int) int {
	return c.Calculate(factor, 10)
}

func Round(yen int) int {
	return yen / 10 * 10
}

func AddTax(yen int) int {
	return yen + yen/10
}
