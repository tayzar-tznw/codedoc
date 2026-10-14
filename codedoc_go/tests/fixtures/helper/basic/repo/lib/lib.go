package lib

import "fmt"

type Greeter interface {
	Greet() string
}

type English struct{}

func (English) Greet() string { return "hello" }

type Base struct{}

func (Base) Hello() string { return "base" }

type Child struct {
	Base
}

func Say(g Greeter) string {
	return g.Greet()
}

func Format(v int) string {
	return fmt.Sprint(v)
}

func Seq(yield func(int) bool) {
	yield(1)
}

func Total(xs []int) int {
	n := 0
	for _, x := range xs {
		n += x
	}
	return n
}
