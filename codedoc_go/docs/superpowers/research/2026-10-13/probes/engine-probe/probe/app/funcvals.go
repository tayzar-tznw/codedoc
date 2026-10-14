package main

import (
	"sync"

	"example.com/lib"
	"example.com/lib/trace"
)

var fnVar = lib.Hello

type holder struct{ fn func() string }

func localHello() string {
	trace.Hit()
	return "local"
}

func localDefer() {
	trace.Hit()
}

func goTarget(wg *sync.WaitGroup) {
	trace.Hit()
	wg.Done()
}

func callParam(f func() string) {
	trace.Hit()
	f() //@FV03
}

func funcValues() {
	fnVar() //@FV01
	h := holder{fn: lib.Hello}
	h.fn() //@FV02
	callParam(localHello)
	handlers := map[string]func() string{"a": lib.Hello, "b": localHello}
	handlers["a"]() //@FV04
	fs := []func() string{localHello}
	fs[0]() //@FV05
	cl := func() string {
		trace.Hit()
		return "closure"
	}
	cl() //@FV06
	func() { trace.Hit() }() //@FV07
	defer localDefer() //@FV08
	defer func() { trace.Hit() }() //@FV09
	var wg sync.WaitGroup
	wg.Add(2)
	go goTarget(&wg) //@FV10
	go func() { trace.Hit(); wg.Done() }() //@FV11
	wg.Wait()
	op := lib.Hello
	op = localHello
	op() //@FV12
}
