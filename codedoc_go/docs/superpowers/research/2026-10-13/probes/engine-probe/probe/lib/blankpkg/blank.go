package blankpkg

import (
	"example.com/lib"
	"example.com/lib/trace"
)

var Loaded bool

func init() {
	trace.Hit()
	lib.Hello() //@IN04
	Loaded = true
}
