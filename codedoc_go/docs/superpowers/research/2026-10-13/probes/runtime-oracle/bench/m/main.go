package main

import (
	"fmt"
	"os"
	"time"

	rt "codedoc.dev/goracle/rt"
)

type I interface{ M(int) int }
type T struct{}

func (T) M(x int) int { rt.E(3); return x + 1 }

func leaf(x int) int  { rt.E(1); return x + 1 }
func plain(x int) int { return x + 1 }

func main() {
	const n = 20_000_000
	var s int
	t0 := time.Now()
	for i := 0; i < n; i++ {
		s += plain(i)
	}
	base := time.Since(t0)
	t0 = time.Now()
	for i := 0; i < n; i++ {
		s += leaf(i)
	}
	hooked := time.Since(t0)
	var it I = T{}
	t0 = time.Now()
	for i := 0; i < n; i++ {
		s += it.M(i)
	}
	iface := time.Since(t0)
	fmt.Printf("slow=%q plain %.1f ns/call, hooked direct %.1f ns/call, hooked via value-method wrapper %.1f ns/call (sum %d)\n",
		os.Getenv("GORACLE_SLOW"), float64(base)/n, float64(hooked)/n, float64(iface)/n, s%7)
	rt.Flush()
}
