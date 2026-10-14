package main

import (
	"example.com/app/internal/helper"
	"example.com/lib"
	L "example.com/lib"
	"example.com/lib/blankpkg"
	. "example.com/lib/dotpkg"
	"example.com/lib/trace"
)

func localFunc() int {
	trace.Hit()
	return 1
}

func pkgCalls() {
	localFunc()   //@PK01
	lib.Hello()   //@PK02
	L.Hello()     //@PK03
	Dotted()      //@PK04
	helper.Help() //@PK05
	_ = blankpkg.Loaded
}
