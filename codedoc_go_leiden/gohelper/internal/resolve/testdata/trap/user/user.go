package user

import "example.com/trap/defs"

func Use(l defs.Logger) string { return l.Log(nil) }

type Namer interface{ Name() string }

type N struct{}

func (N) Name() string { return "n" }

func Call(n Namer) string { return n.Name() }
