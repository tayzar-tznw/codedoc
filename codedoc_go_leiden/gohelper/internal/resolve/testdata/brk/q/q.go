package q

import (
	"example.com/brk/bi"
	"example.com/brk/d"
	"example.com/brk/ifc"
	"example.com/brk/imp"
	"example.com/brk/inv"
	"example.com/brk/p"
	"example.com/brk/pcx"
	"example.com/brk/pf"
	"example.com/brk/r"
	"example.com/brk/t"
)

func Use() string { return p.S{}.M() }

func Alias() string {
	f := p.S{}.M
	return f()
}

func Expr() string { return p.S.M(p.S{}) }

func Ignored() string { return pcx.S{}.M() }

func Dropped() string { return d.D{}.M() }

func Unattached() string { return t.S{}.M() }

func ImportOnly() string { return imp.S{}.M() }

func Clean() string { return r.Wrapper{}.M() }

func Fixed() string { return pf.S{}.M() }

func Dispatch(i ifc.I) string { return i.M() }

func BrokenIface(j bi.J) string { return j.M() }

func EmbedsBroken(k ifc.K) string { return k.M() }

func UseReady(x inv.Ready) (*inv.Manager, error) { return x.Get() }
