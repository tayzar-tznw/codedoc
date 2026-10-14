package static

import "strings"

func Plain() string { return "plain" }

type Counter struct{ n int }

func (c Counter) Value() int { return c.n }

func (c *Counter) Inc() { c.n++ }

type Base struct{}

func (Base) Hello() string { return "base" }

func (Base) Shadowed() string { return "base" }

type Mid struct{ Base }

func (Mid) Shadowed() string { return "mid" }

type Top struct{ Mid }

type List[T any] struct{ items []T }

func (l *List[T]) Push(v T) { l.items = append(l.items, v) }

type Alias[T any] = List[T]

func Identity[T any](v T) T { return v }

func Calls() {
	_ = Plain()
	c := Counter{}
	_ = c.Value()
	c.Inc()
	p := &c
	_ = p.Value()
	var t Top
	_ = t.Hello()
	_ = t.Shadowed()
	var m Mid
	_ = m.Hello()
	_ = Counter.Value(c)
	(*Counter).Inc(p)
	_ = Identity(3)
	_ = Identity[string]("x")
	var l List[int]
	l.Push(1)
	var a Alias[int]
	a.Push(2)
	_ = strings.ToUpper("x")
	_ = (Plain)()
}
