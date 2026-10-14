package rt

import (
	"fmt"
	"runtime"
)

//go:noinline
func E(tag string) {
	var pcs [2]uintptr
	n := runtime.Callers(2, pcs[:])
	fr := runtime.CallersFrames(pcs[:n])
	callee, _ := fr.Next()
	caller, _ := fr.Next()
	fmt.Printf("%-18s callee=%s | caller=%s @ %s:%d\n", tag, callee.Function, caller.Function, caller.File, caller.Line)
}
