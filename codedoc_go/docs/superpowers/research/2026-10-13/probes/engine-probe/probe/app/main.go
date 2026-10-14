package main

import (
	"fmt"

	"example.com/lib/testy"
	"example.com/lib/trace"
)

func main() {
	pkgCalls()
	methods()
	embedding()
	ifaces()
	generics()
	funcValues()
	notCalls()
	thirdParty()
	fmt.Println(platform())
	lowLevel()
	fmt.Println(generatedHelper()) //@GC02
	encoding()
	fmt.Println(testy.CallHook(testy.Outer{}))
	trace.Dump()
}
