package stubemb

import (
	"example.com/thirdparty/zap"
	"example.com/trap/logutil"
)

type Inner struct{}

func (Inner) SetT(x any) string { return "inner" }

type Mid struct{ Inner }

type Outer struct {
	Mid
	logutil.Thing
}

func F(o Outer) string { return o.SetT(nil) }

func Alias(o Outer) string {
	f := o.SetT
	return f(nil)
}

type Setter interface{ SetT(x any) string }

func Iface(s Setter) string { return s.SetT(nil) }

var _ = zap.New
