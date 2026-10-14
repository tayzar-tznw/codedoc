package lib

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"strings"
)

var Shout = func(s string) string { return strings.ToUpper(s) + "!" }

var order []string

func init() { order = append(order, "first") }

func init() { order = append(order, Note("second")) }

func Note(s string) string { return s }

func Order() string { return strings.Join(order, ",") }

type Animal interface {
	Sound() string
}

type Dog struct{}

func (Dog) Sound() string { return "woof" }

type Cat struct{ name string }

func (c *Cat) Sound() string { return c.name + " meow" }

func Chorus(as ...Animal) string {
	out := ""
	for _, a := range as {
		out += a.Sound()
	}
	return out
}

func Seq(n int) func(func(int) bool) {
	return func(yield func(int) bool) {
		for i := 0; i < n; i++ {
			if !yield(i) {
				return
			}
		}
	}
}

func SumSeq(n int) int {
	seq := Seq(n)
	total := 0
	for v := range seq {
		total += Double(v)
	}
	return total
}

func Double(x int) int { return 2 * x }

type Counter struct{ n int }

func (c *Counter) All(yield func(int) bool) {
	for i := 0; i < c.n; i++ {
		if !yield(i) {
			return
		}
	}
}

func (c *Counter) Sum() int {
	s := 0
	for v := range c.All {
		s += v
	}
	return s
}

func Max[T int | float64](a, b T) T {
	if a > b {
		return a
	}
	return b
}

type Box[T any] struct{ v T }

func (b *Box[T]) Get() T { return b.v }

func Cleanup(log *[]string, extra bool) (err error) {
	defer func() {
		if r := recover(); r != nil {
			err = errors.New(fmt.Sprint(r))
		}
	}()
	if extra {
		defer record(log, "extra")
	}
	*log = append(*log, "body")
	if len(*log) > 100 {
		return nil
	}
	panic("boom")
}

func record(log *[]string, s string) { *log = append(*log, s) }

func Write(w io.Writer, s string) int {
	n, _ := w.Write([]byte(s))
	return n
}

type upper struct{ buf strings.Builder }

func (u *upper) Write(p []byte) (int, error) { return u.buf.Write(bytes.ToUpper(p)) }

func Writers() string {
	u := &upper{}
	Write(u, "a")
	var b bytes.Buffer
	Write(&b, "b")
	return u.buf.String() + b.String()
}

func Pick(k string) string {
	if k == "a" {
		return Note("A")
	} else if k == "b" {
		return Note("B")
	}
	switch strings.ToLower(k) {
	case "c":
		return Note("C")
	}
	return ""
}

func Both(a, b bool) bool { return a && Check(b) }

func Check(b bool) bool { return b }

type Ops struct{ F func(int) int }

func Apply(o Ops, m map[string]func(int) int, x int) int {
	return o.F(x) + m["inc"](x)
}

func Spawn() int {
	done := make(chan int, 2)
	go func() { done <- Double(1) }()
	go send(done, 3)
	total := 0
loop:
	for i := 0; i < 2; i++ {
		select {
		case v := <-done:
			total += v
		}
		if i > 5 {
			break loop
		}
	}
	return total
}

func send(c chan int, v int) { c <- v }
