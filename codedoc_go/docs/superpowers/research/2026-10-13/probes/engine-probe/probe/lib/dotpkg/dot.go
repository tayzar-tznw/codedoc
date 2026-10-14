package dotpkg

import "example.com/lib/trace"

func Dotted() string {
	trace.Hit()
	return "dotted"
}
