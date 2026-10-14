package main

import (
	"fmt"

	"example.com/gp/lib"
)

func main() {
	s := lib.New()
	x := "日本語"; fmt.Println(s.Get(x))
	b := &lib.Box[int]{}
	_ = b.Val()
	_ = b.Map[string](func(i int) string { return fmt.Sprint(i) })
	p := new(len(x))
	f := func() int { return *p }
	_ = f()
}
