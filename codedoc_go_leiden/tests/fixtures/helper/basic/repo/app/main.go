package main

import (
	"strings"

	"example.com/lib"
)

func main() {
	c := lib.Child{}
	_ = c.Hello()
	_ = lib.Say(lib.English{})
	_ = strings.ToUpper("x")
	f := lib.Format
	_ = f(1)
	func() {}()
	for n := range lib.Seq {
		_ = n
	}
	_ = []byte("x")
	_ = len("x")
	_ = lib.Native()
	var g lib.Greeter = lib.English{}
	_ = g.Greet()
	_ = any(c).(interface{ Hello() string }).Hello()
	_ = lib.Total([]int{1, 2})
}
