package main

import (
	_ "unsafe"

	"example.com/app/internal/asm"
	"example.com/app/internal/cg"
	_ "example.com/lib/hidden"
)

//go:linkname runtimeNanotime runtime.nanotime
func runtimeNanotime() int64

//go:linkname hiddenSecret example.com/lib/hidden.secret
func hiddenSecret() string

func lowLevel() {
	_ = cg.Add()         //@BC06
	_ = asm.Add(1, 2)    //@BC07
	_ = runtimeNanotime() //@BC08
	_ = hiddenSecret()   //@BC09
}
