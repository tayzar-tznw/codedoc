//go:build integration

package main

import "example.com/lib"

func integ() string {
	return lib.Hello() + localIntegOnly() //@BC03
}

func localIntegOnly() string { return "integ" }
