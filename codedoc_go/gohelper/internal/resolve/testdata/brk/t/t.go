package t

import "example.com/brk/r"

type S struct{ r.Base }

func (s *Sx) M() string { return "s" }
