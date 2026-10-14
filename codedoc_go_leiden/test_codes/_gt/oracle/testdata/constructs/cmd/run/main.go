package main

import (
	"fmt"

	"example.com/constructs/lib"
)

func main() {
	fmt.Println(lib.Shout("hi"), lib.Order())
	fmt.Println(lib.Chorus(lib.Dog{}, &lib.Cat{}), lib.SumSeq(3))
	c := &lib.Counter{}
	fmt.Println(c.Sum(), lib.Max(2, 3), (&lib.Box[int]{}).Get())
	var log []string
	fmt.Println(lib.Cleanup(&log, false), log)
	fmt.Println(lib.Writers(), lib.Pick("a"), lib.Pick("b"), lib.Pick("C"), lib.Both(true, true), lib.Both(false, true))
	inc := func(x int) int { return x + 1 }
	fmt.Println(lib.Apply(lib.Ops{F: lib.Double}, map[string]func(int) int{"inc": inc}, 4), lib.Spawn())
	sound := lib.Dog{}.Sound
	fmt.Println(sound(), (*lib.Cat).Sound(&lib.Cat{}))
	lib.Recovered()
	lib.Looping()
	fmt.Println(lib.PtrDefer(), lib.Size(), lib.Made)
	fmt.Println(lib.Defers())
}
