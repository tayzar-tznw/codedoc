package main

import "example.com/lib/embed"

func embedding() {
	var o embed.One
	o.Hello() //@EM01
	var t embed.Top
	t.Hello()    //@EM02
	t.Shadowed() //@EM03
	pe := embed.PtrEmb{Base: &embed.Base{}}
	pe.PtrM()  //@EM04
	pe.Hello() //@EM05
	w := embed.WithIface{Greeter: embed.English{}}
	w.Greet() //@EM06
	rc := embed.NewRC()
	rc.Read()          //@EM07
	rc.Close()         //@EM08
	t.Mid.Base.Hello() //@EM09
	t.Base.Shadowed()  //@EM10
}
