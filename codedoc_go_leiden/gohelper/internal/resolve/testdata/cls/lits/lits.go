package lits

import "fmt"

type Bag struct{ items []int }

func (b *Bag) All(yield func(int) bool) {
	for _, v := range b.items {
		if !yield(v) {
			return
		}
	}
}

func Count(n int) func(func(int) bool) {
	return func(yield func(int) bool) {
		for i := 0; i < n; i++ {
			if !yield(i) {
				return
			}
		}
	}
}

func Seq(yield func(int) bool) {}

type MyFunc func() string

func hello() string { return "h" }

type Celsius float64

func Calls(b *Bag, xs []int) {
	func() {}()
	go func() {}()
	defer func() {}()
	for v := range b.All {
		_ = v
	}
	for v := range Seq {
		_ = v
	}
	for v := range Count(3) {
		_ = v
	}
	for _, x := range xs {
		_ = x
	}
	_ = int32(3)
	_ = []byte("x")
	_ = MyFunc(hello)
	_ = Celsius(1.5)
	p := &b.items
	_ = (*[]int)(p)
	_ = (func() string)(hello)
	_ = len(xs)
	_ = append(xs, 1)
	_ = min(1, 2)
	_ = fmt.Sprint(1)
	go hello()
	defer hello()
}

type Vec[T any] []T

var table [len("abcd")]int

func More(xs []int) [len("ab")]byte {
	_ = Vec[int](xs)
	var buf [len("xyz")]byte
	_ = buf
	return [len("ab")]byte{}
}
