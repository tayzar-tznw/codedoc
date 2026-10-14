package hidden

import (
	_ "unsafe"

	"example.com/lib/trace"
)

//go:linkname secret
func secret() string {
	trace.Hit()
	return "secret"
}
