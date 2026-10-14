package main

import (
	"fmt"
	"io"
	"log"

	"example.com/lib/iface"
	"example.com/lib/trace"
)

func ifaces() {
	var z iface.Zero
	if z != nil {
		z.Nothing() //@IF01
	}
	var s iface.Single = iface.OnlyImpl{}
	s.Only() //@IF02
	for _, m := range []iface.Multi{iface.A{}, &iface.B{}} {
		m.Do() //@IF03
	}
	var lg iface.Logger = log.New(io.Discard, "", 0)
	trace.Dyn("IF04", fmt.Sprintf("%T", lg))
	lg.Printf("x") //@IF04
	lg = iface.MyLog{}
	lg.Printf("y") //@IF05
	var w io.Writer = &iface.Buf{}
	w.Write([]byte("z")) //@IF06
	fmt.Fprintf(w, "q")  //@IF07
	var se iface.Sealed = iface.S1{}
	se.Name() //@IF08
	var anyv any = iface.S2{}
	switch v := anyv.(type) {
	case iface.S1:
		v.Name() //@IF09
	case iface.S2:
		v.Name() //@IF10
	}
	if n, ok := anyv.(iface.Sealed); ok {
		n.Name() //@IF11
	}
	anyv.(iface.S2).Name() //@IF12
	iface.Multi.Do(iface.A{}) //@IF13
	md := se.Name
	md() //@IF14
	if c, ok := anyv.(interface{ Name() string }); ok {
		c.Name() //@IF15
	}
}
