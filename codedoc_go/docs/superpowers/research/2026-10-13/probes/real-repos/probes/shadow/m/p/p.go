package p

import "github.com/stretchr/testify/suite"

// Inner.SetT is reachable from Outer at depth 2 (through Mid).
type Inner struct{}

func (Inner) SetT(x int) string { return "inner" }

type Mid struct{ Inner }

// Outer embeds suite.Suite, whose SetT (depth 1) shadows Inner.SetT (depth 2) when the module is available.
type Outer struct {
	Mid
	suite.Suite
}

func F(o *Outer) { o.SetT(nil) }
