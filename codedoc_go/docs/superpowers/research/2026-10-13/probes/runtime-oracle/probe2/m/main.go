package main

import rt "codedoc.dev/goracle/rt"

func Map[T, U any](xs []T, f func(T) U) []U { rt.E("Map"); var o []U; for _, x := range xs { o = append(o, f(x)) }; return o }

type Box[T any] struct{ v T }

func (b *Box[T]) Get() T { rt.E("(*Box).Get"); return b.v }
func (b Box[T]) Val() T  { rt.E("Box.Val"); return b.v }

type Getter[T any] interface{ Get() T }

func small(x int) int { rt.E("small"); return x + 1 }

func main() {
	f := Map[int, int]
	_ = f([]int{1}, small)
	b := &Box[int]{v: 1}
	g := b.Get
	_ = g()
	var gi Getter[int] = b
	_ = gi.Get()
	h := (*Box[int]).Get
	_ = h(b)
	vv := Box[string]{}
	vf := vv.Val
	_ = vf()
	_ = small/*line /W/abs/main.go#77:30:11*/(/*line /home/admin/scratch/codedoc-go-research/runtime-oracle/probe2/m/main.go:30:12*/1) + small(2)
}
