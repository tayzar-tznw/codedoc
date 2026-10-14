//go:build !cgo

package cg

import "example.com/lib/trace"

func Add() int {
	trace.Hit()
	return 3
}
