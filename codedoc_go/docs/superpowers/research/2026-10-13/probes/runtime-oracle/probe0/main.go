package main

import (
	"fmt"
	"reflect"
	"sort"
	"sync"
)

var gv = initVal()

func initVal() int { hk("initVal"); return 1 }

func init() { hk("init"); plain() }

func plain() { hk("plain") }

func withArg(x int) { hk("withArg") }

func b() int { hk("b"); return 1 }
func c() int { hk("c"); return 2 }
func a(x, y int) int { hk("a"); return x + y }

type V struct{ n int }

func (v V) ValM()   { hk("V.ValM") }
func (v *V) PtrM()  { hk("(*V).PtrM") }

type Inner struct{}

func (Inner) InnerM()   { hk("Inner.InnerM") }

type PInner struct{}

func (*PInner) PInnerM() { hk("(*PInner).PInnerM") }

type Outer struct {
	Inner
	*PInner
}

type Shape interface{ Area() float64 }
type Circle struct{ r float64 }
type Square struct{ s float64 }

func (c Circle) Area() float64  { hk("Circle.Area"); return c.r }
func (s *Square) Area() float64 { hk("(*Square).Area"); return s.s }

type InnerI interface{ InnerM() }

func Map[T, U any](xs []T, f func(T) U) []U {
	hk("Map[T,U]")
	var out []U
	for _, x := range xs {
		out = append(out, f(x))
	}
	return out
}

type Stack[T any] struct{ items []T }

func (s *Stack[T]) Push(x T) { hk("(*Stack[T]).Push"); s.items = append(s.items, x) }

type Name string

func (n Name) String() string { hk("Name.String"); return string(n) }

func Str[T fmt.Stringer](t T) string { hk("Str[T]"); return t.String() }

type S struct{ fn func() }

func seq(yield func(int) bool) {
	hk("seq")
	for i := 0; i < 2; i++ {
		if !yield(i) {
			return
		}
	}
}

func deferrer() {
	hk("deferrer")
	defer plain()
	defer withArg(7)
	v := &V{}
	defer v.PtrM()
	defer func() { hk("deferrer.lit") }()
	b()
}

func panicker() {
	hk("panicker")
	defer func() {
		hk("panicker.recover")
		if r := recover(); r != nil {
			_ = r
		}
	}()
	defer withArg(9)
	var p *V
	p.boom()
}

func (v *V) boom() { hk("(*V).boom"); _ = v.n }

func main() {
	hk("main")
	plain()
	_ = a(b(), c())
	v := V{}
	p := &V{}
	v.ValM()
	p.PtrM()
	p.ValM()
	v.PtrM()
	o := Outer{PInner: &PInner{}}
	o.InnerM()
	o.PInnerM()
	var ii InnerI = o
	ii.InnerM()
	for _, s := range []Shape{Circle{1}, &Square{2}} {
		s.Area()
	}
	_ = Map([]int{1}, func(i int) string { hk("Map.lit"); return "" })
	st := &Stack[int]{}
	st.Push(1)
	_ = Str(Name("x"))
	f := func() { hk("named.lit") }
	f()
	func() { hk("iife.lit") }()
	mv := p.PtrM
	mv()
	vmv := v.ValM
	vmv()
	(*V).PtrM(p)
	V.ValM(v)
	(*V).ValM(p)
	s := S{fn: plain}
	s.fn()
	m := map[string]func(){"a": plain}
	m["a"]()
	var sh Shape = Circle{3}
	af := sh.Area
	af()
	deferrer()
	panicker()
	var wg sync.WaitGroup
	wg.Add(3)
	go func() { defer wg.Done(); hk("go.lit") }()
	go withArgDone(&wg, 5)
	go plainDone(&wg)
	wg.Wait()
	reflect.ValueOf(plain).Call(nil)
	reflect.ValueOf(p).MethodByName("PtrM").Call(nil)
	xs := []int{3, 1, 2}
	sort.Slice(xs, func(i, j int) bool { hk("sort.less"); return xs[i] < xs[j] })
	for x := range seq {
		hk("range.body")
		_ = x
	}
}

func withArgDone(wg *sync.WaitGroup, x int) { hk("withArgDone"); wg.Done() }

var pwg *sync.WaitGroup

func plainDone(wg *sync.WaitGroup) { hk("plainDone"); wg.Done() }
