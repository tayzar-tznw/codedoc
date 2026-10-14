package crossa

import "example.com/trap/crossb"

type A struct{ crossb.S }

func F(x A) { x.Info("x") }
