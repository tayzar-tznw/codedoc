package shadow

import (
	"example.com/thirdparty/suite"
	"example.com/thirdparty/zap"
)

type Inner struct{}

func (Inner) SetT(x any) string { return "inner" }

type Mid struct{ Inner }

type Outer struct {
	Mid
	suite.Suite
}

func F(o *Outer) string { return o.SetT(nil) }

func Direct(m Mid) string { return m.SetT(nil) }

type Sink interface {
	zap.Core
	Flush()
}

type Deep struct{}

func (Deep) Write(s string) {}

type Mid2 struct{ Deep }

type Outer2 struct {
	Mid2
	Sink
}

func G(o Outer2) { o.Write("x") }

type Greeter interface{ Greet() string }

type Hello struct{}

func (Hello) Greet() string { return "hello" }

func Greet(g Greeter) string { return g.Greet() }

func Ranges() int {
	n := 0
	for range zap.Levels {
		n++
	}
	for x := range zap.Seq {
		n += x
	}
	return n
}
