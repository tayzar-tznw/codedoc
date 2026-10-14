//go:build ignore

package main

import "example.com/lib"

func main() {
	lib.Hello() //@BC04
}
