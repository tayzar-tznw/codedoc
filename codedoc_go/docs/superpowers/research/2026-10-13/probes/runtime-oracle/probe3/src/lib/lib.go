package lib

import (
	"bytes"
	"fmt"
	"io"
	"reflect"
	"sort"
	"sync"

	dotpkg "example.com/probe3/v1.x"
)

var Registry = map[string]func() int{"one": One, "two": func() int { return Two() }}

var initialized = setup()

func setup() bool { return One() == 1 }

func init() { _ = One() }

func One() int { return 1 }
func Two() int { return 2 }
func Add(x, y int) int { return x + y }

type V struct{ n int }

func (v V) Val() int  { return v.n }
func (v *V) Ptr() int { return v.n }

type Inner struct{}

func (Inner) Hello() string { return "inner" }

type Outer struct{ Inner }

type Shape interface{ Area() float64 }
type Circle struct{ R float64 }
type Square struct{ S float64 }

func (c Circle) Area() float64  { return c.R }
func (s *Square) Area() float64 { return s.S }

func Total(shapes []Shape) float64 {
	t := 0.0
	for _, s := range shapes {
		t += s.Area()
	}
	return t
}

type Stack[T any] struct{ items []T }

func (s *Stack[T]) Push(x T) { s.items = append(s.items, x) }

func Map[T, U any](xs []T, f func(T) U) []U {
	var out []U
	for _, x := range xs {
		out = append(out, f(x))
	}
	return out
}

type Name string

func (n Name) String() string { return string(n) }

func Str[T fmt.Stringer](t T) string { return t.String() }

type Holder struct{ Fn func() int }

func Seq(n int) func(func(int) bool) {
	return func(yield func(int) bool) {
		for i := 0; i < n; i++ {
			if !yield(i) {
				return
			}
		}
	}
}

func Count(n int) int {
	c := 0
	for x := range Seq(n) {
		c += Add(x, 0)
	}
	return c
}

func Nested() int {
	outer := func() int {
		inner := func() int { return One() }
		return inner()
	}
	return outer()
}

type myWriter struct{ n int }

func (w *myWriter) Write(p []byte) (int, error) { w.n += len(p); return len(p), nil }

func WriteTo(w io.Writer, s string) { w.Write([]byte(s)) }

func Writers() int {
	mw := &myWriter{}
	WriteTo(mw, "a")
	var b bytes.Buffer
	WriteTo(&b, "b")
	return mw.n + b.Len()
}

var mu sync.Mutex

func unlock() { mu.Unlock() }

func Deferred(x int) int {
	mu.Lock()
	defer unlock()
	if x > 0 {
		return Add(x, One())
	}
	return Two()
}

func TwoDefers() (r int) {
	defer func() { r += One() }()
	defer unlock()
	mu.Lock()
	return Two()
}

func OneLiner() int { defer unlock(); mu.Lock(); return One() }

func Recovering() (err error) {
	defer func() {
		if r := recover(); r != nil {
			err = fmt.Errorf("recovered: %v", r)
		}
	}()
	defer unlock()
	mu.Lock()
	var p *V
	return fmt.Errorf("%d", p.Ptr())
}

func Spawn() int {
	var wg sync.WaitGroup
	wg.Add(3)
	res := make([]int, 3)
	go func() { defer wg.Done(); res[0] = One() }()
	go fill(&wg, res, 1)
	go fill(&wg, res, Two())
	wg.Wait()
	return res[0] + res[1] + res[2]
}

func fill(wg *sync.WaitGroup, res []int, i int) { defer wg.Done(); res[i%3] = i }

func Dispatch() int {
	v := V{1}
	p := &V{2}
	o := Outer{}
	total := v.Val() + p.Ptr() + p.Val() + v.Ptr()
	_ = o.Hello()
	var h interface{ Hello() string } = o
	_ = h.Hello()
	total += int(Total([]Shape{Circle{1}, &Square{2}}))
	st := &Stack[int]{}
	st.Push(1)
	_ = Map([]int{1, 2}, func(i int) string { return fmt.Sprint(i) })
	_ = Str(Name("x"))
	named := func() int { return One() }
	total += named()
	total += func() int { return Two() }()
	mv := p.Ptr
	total += mv()
	me := (*V).Ptr
	total += me(p)
	total += V.Val(v)
	hd := Holder{Fn: Two}
	total += hd.Fn()
	total += Registry["one"]() + Registry["two"]()
	total += Add(One(), Two())
	r := reflect.ValueOf(One).Call(nil)
	total += int(r[0].Int())
	xs := []int{3, 1, 2}
	sort.Slice(xs, func(i, j int) bool { return xs[i] < xs[j] })
	total += Count(3) + Nested() + Writers() + Deferred(1) + Deferred(0) + TwoDefers() + OneLiner() + Spawn()
	_ = Recovering()
	total += dotpkg.Dotted()
	if initialized {
		total++
	}
	return total
}
