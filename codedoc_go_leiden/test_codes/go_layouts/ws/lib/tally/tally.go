package tally

type Counter struct {
	n int
}

func New() *Counter {
	return &Counter{}
}

func (c *Counter) Add(k int) {
	c.n = Sum(c.n, k)
}

func (c *Counter) Total() int {
	return c.n
}

func Sum(a, b int) int {
	return a + b
}
