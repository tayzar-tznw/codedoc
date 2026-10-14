package imp

import (
	"example.com/brk/r"
	"example.com/missing/zap"
)

type S struct {
	r.Base
	lg *zap.Logger
}

func (s S) Log() { s.lg.Info("x") }
