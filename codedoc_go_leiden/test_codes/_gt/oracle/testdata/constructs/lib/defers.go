package lib

import "strings"

func cleanupArg(n int) {}
func compute2() int    { return 2 }
func compute3() int    { return 3 }

type closer struct{}

func (closer) Close() {}

func WrappedRecovered() {
	defer cleanupArg(1)
	defer func() { recover() }()
	panic(compute2())
}

func WrappedMethod(w closer) {
	defer w.Close()
	defer func() { recover() }()
	panic(compute3())
}

func cleanupR()  {}
func cleanupR2() {}

func Seq3(yield func(int) bool) {
	for i := 0; i < 3; i++ {
		if !yield(i) {
			return
		}
	}
}

func RangeReturn(early bool) int {
	defer cleanupR()
	if early {
		defer cleanupR2()
		for x := range Seq3 {
			return x
		}
	}
	return 0
}

type Closer2 interface{ Close() }

type ca struct{}

func (ca) Close() {}

type cb struct{}

func (cb) Close() {}

func cleanupG() {}

func GroupIface(c Closer2) {
	defer c.Close()
	defer cleanupG()
}

func GroupIface2(d Closer2) {
	defer d.Close()
	defer cleanupG()
}

type Sink interface {
	WriteString(s string) (int, error)
}

func Emit(s Sink, k int) bool {
	if k > 0 {
		return false
	} else if n, _ := s.WriteString("x"); n > 0 {
		return true
	}
	return false
}

func Defers() int {
	WrappedRecovered()
	WrappedMethod(closer{})
	n := RangeReturn(false) + RangeReturn(true)
	GroupIface(ca{})
	GroupIface2(cb{})
	if Emit(&strings.Builder{}, 0) {
		n++
	}
	return n
}
