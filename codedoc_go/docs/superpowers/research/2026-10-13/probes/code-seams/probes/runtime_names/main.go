package main

import (
	"fmt"

	"example.com/rn/sub"
)

func main() {
	var t sub.T
	out := append([]string{}, sub.Names...)
	out = append(out, sub.Hook(), sub.Hook2(), t.Val(), t.Ptr(), sub.G[int]{}.Val(), (&sub.G[string]{}).Ptr(),
		t.Gen(1), t.Gen("s"), sub.F(), t.MClos(), sub.Gen(1), sub.Gen("x"), sub.GenClos(2.5))
	out = append(out, sub.Clos()...)
	for _, n := range out {
		fmt.Println(n)
	}
}
