package main

import (
	"strconv"

	"example.com/lib/gen"
	"example.com/lib/trace"
)

func generics() {
	gen.Identity[int](1)            //@GE01
	gen.Identity("s")               //@GE02
	gen.Map([]int{1}, strconv.Itoa) //@GE03
	l := &gen.List[int]{}
	l.Push(1) //@GE04
	var a gen.Alias[string]
	a.Push("x")                     //@GE05
	gen.Describe(gen.Named{N: "n"}) //@GE06
	gen.Describe(gen.Other{})       //@GE07
	for x := range gen.Count(2) {   //@GE08
		trace.Hit()
		_ = x
	}
	b := &gen.Bag{}
	for x := range b.All { //@GE09
		trace.Hit()
		_ = x
	}
	bx := gen.Box[int]{V: 1}
	bx.Apply(strconv.Itoa)          //@GE10
	var idf func(int) int = gen.Identity
	idf(3)                          //@GE12
	gen.Sum(1, 2)                   //@GE18
	var w gen.Wrapped[int]
	w.Push(2) //@GE19
}
