package pf

import "example.com/brk/r"

type S struct{ r.Base }

func (S) M() string { return "s" }
