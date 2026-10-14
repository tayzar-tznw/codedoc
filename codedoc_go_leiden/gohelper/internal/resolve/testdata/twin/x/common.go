package common

type Inner struct{}

func (Inner) SetT(x any) string { return "inner" }

type Mid struct{ Inner }

type Outer struct {
	Mid
	Ext
}

func F(o *Outer) string { return o.SetT(nil) }
