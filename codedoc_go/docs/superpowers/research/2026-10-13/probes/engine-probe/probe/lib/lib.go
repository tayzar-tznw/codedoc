package lib

import "example.com/lib/trace"

func Hello() string {
	trace.Hit()
	return "hello"
}

type Counter struct{ n int }

func NewCounter() *Counter {
	trace.Hit()
	return &Counter{}
}

func (c Counter) Value() int {
	trace.Hit()
	return c.n
}

func (c *Counter) Inc() {
	trace.Hit()
	c.n++
}

type Celsius float64

func (c Celsius) F() float64 {
	trace.Hit()
	return float64(c)*9/5 + 32
}

type Names []string

func (n Names) Len() int {
	trace.Hit()
	return len(n)
}
