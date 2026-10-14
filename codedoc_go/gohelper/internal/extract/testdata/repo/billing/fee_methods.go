package billing

func (c *FeeCalculator) Total() int {
	return c.Calculate(1)
}
