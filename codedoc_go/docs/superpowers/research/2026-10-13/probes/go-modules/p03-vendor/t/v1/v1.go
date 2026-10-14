package v1

import (
	"example.com/dep"
	"example.com/dep/sub"
	dep2 "example.com/dep/v2"
	upper "example.com/Upper"
	"example.com/loc"
)

func F() string { loc.L(); upper.U(); return dep.Version() + sub.Sub() + dep2.Version() }
