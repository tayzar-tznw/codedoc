package stubalias

import (
	"example.com/thirdparty/zap"
	"example.com/trap/logutil"
)

type Inner struct{}

func (Inner) SetT(x any) string { return "inner" }

type Mid struct{ Inner }

type TA = logutil.Thing

type OuterPtr struct {
	Mid
	*TA
}

type OuterVal struct {
	Mid
	TA
}

func FP(o OuterPtr) string { return o.SetT(nil) }

func FV(o OuterVal) string { return o.SetT(nil) }

var _ = zap.New
