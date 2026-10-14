package main

import (
	"example.com/review2/k"
	"example.com/review2/m"
	"example.com/review2/n"
	"example.com/review2/q"
	"example.com/review2/r"
	"example.com/review2/s"
	"example.com/review2/v"
	"example.com/review2/w"
)

func main() {
	println(q.Use(q.F{}))
	v.ViaItab(v.A{})
	v.MethodValue(v.A{})
	v.Promoted(v.Outer{})
	k.Loop()
	println(n.Use(n.A{}))
	println(m.MustFail())
	w.F()
	w.E()
	r.F()
	println(s.Pair())
}
