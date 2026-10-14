package testy

import "example.com/lib/trace"

var InternalHelper = internalHelper

func ExportedForTest() int {
	trace.Hit()
	return internalHelper() //@TS07
}
