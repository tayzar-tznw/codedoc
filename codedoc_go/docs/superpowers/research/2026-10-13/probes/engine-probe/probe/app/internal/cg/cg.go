//go:build cgo

package cg

/*
static int cadd(int a, int b) { return a + b; }
*/
import "C"

import "example.com/lib/trace"

func Add() int {
	trace.Hit()
	return int(C.cadd(1, 2)) //@BC05
}
