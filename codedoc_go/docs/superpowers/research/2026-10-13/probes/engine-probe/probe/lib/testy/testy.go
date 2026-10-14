package testy

import "example.com/lib/trace"

type Clock interface{ Now() int }

type realClock struct{}

func (realClock) Now() int {
	trace.Hit()
	return 1
}

var nowFn = defaultNow

func defaultNow() int {
	trace.Hit()
	return 42
}

func internalHelper() int {
	trace.Hit()
	return 7
}

func Stamp(c Clock) int {
	trace.Hit()
	return c.Now() //@TS05
}

func Tick() int {
	trace.Hit()
	return nowFn() //@TS06
}

func Default() Clock { return realClock{} }

type inner struct{}

func (inner) Hook() string {
	trace.Hit()
	return "inner"
}

type Outer struct{ inner }

func CallHook(o Outer) string {
	trace.Hit()
	return o.Hook() //@TS11
}
