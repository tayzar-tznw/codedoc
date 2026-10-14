package main

import "sync"

type B struct{}

func (b *B) Add(n int) *B { hk("Add"); return b }

func g() int  { hk("g"); return 1 }
func u()      { hk("u") }
func w(x int) { hk("w") }

func multiline() {
	b := &B{}
	b.
		Add(1).
		Add(
			2,
		)
	_ = g() +
		g()
}

func retCall() int {
	defer u()
	defer w(1)
	if g() > 5 {
		return 0
	}
	return g()
}

func bareRet() (x int) {
	defer u()
	x = 1
	if x == 1 {
		return
	}
	u()
	return
}

func loopDefer() {
	for i := 0; i < 2; i++ {
		defer w(i)
	}
	g()
}

func tagged() {
	_ = a(/*line main.go#3:51:9*/b/*line main.go#4:51:10*/(/*line main.go:51:11*/), /*line main.go#5:51:15*/c/*line main.go#6:51:16*/(/*line main.go:51:17*/))
}

func taggedLparenOnly() {
	_ = a(b/*line main.go#L1:55:9*/(/*line main.go:55:10*/), c/*line main.go#L2:55:14*/(/*line main.go:55:15*/))
}

func taggedAbs() {
	_ = a(b/*line /abs/x/main.go#L1:59:9*/(/*line /home/admin/scratch/codedoc-go-research/runtime-oracle/probe1/main.go:59:10*/), c())
}

func a(x, y int) int { hk("a"); return x + y }
func b() int        { hk("b"); return 1 }
func c() int        { hk("c"); return 2 }

func goTagged(wg *sync.WaitGroup) {
	go w/*line main.go#G:68:6*/(/*line main.go:68:7*/g())
	go func() { defer wg.Done(); hk("golit") }/*line main.go#GL:69:44*/(/*line main.go:69:45*/)
}

func deferTagged() int {
	defer w/*line main.go#D:73:9*/(/*line main.go:73:10*/1)
	return g/*line main.go#R:74:10*/(/*line main.go:74:11*/)
}

func main() {
	multiline()
	retCall()
	bareRet()
	loopDefer()
	tagged()
	taggedLparenOnly()
	taggedAbs()
	var wg sync.WaitGroup
	wg.Add(1)
	goTagged(&wg)
	wg.Wait()
	deferTagged()
}
