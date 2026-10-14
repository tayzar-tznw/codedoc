package main

import (
	"example.com/lib"
	"example.com/lib/trace"
)

func apply(fn func()) {
	trace.Hit()
	fn() //@ME15
}

func methods() {
	c := lib.Counter{}
	c.Value()               //@ME01
	c.Inc()                 //@ME02
	p := lib.NewCounter()   //@ME03
	p.Value()               //@ME04
	p.Inc()                 //@ME05
	t := lib.Celsius(20)    //@CV01
	t.F()                   //@ME06
	lib.Names{"a"}.Len()    //@ME07
	lib.Counter.Value(c)    //@ME08
	(*lib.Counter).Inc(p)   //@ME09
	(*lib.Counter).Value(p) //@ME10
	f := c.Value            //@ME11
	f()                     //@ME12
	g := p.Inc
	g()          //@ME13
	apply(p.Inc) //@ME14
}
