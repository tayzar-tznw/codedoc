package main

import (
	"unsafe"

	"example.com/lib"
	"example.com/lib/gen"
)

type MyFunc func() string

var initVal = lib.Hello() //@NC01

func init() {
	lib.Hello() //@NC02
}

func init() {
	localHello() //@NC03
}

func notCalls() {
	var x int64 = 5
	_ = int32(x) //@NC04
	p := &lib.Counter{}
	_ = (*lib.Counter)(p)  //@NC05
	_ = []byte("s")        //@NC06
	mf := MyFunc(localHello) //@NC07
	mf()                   //@NC08
	_ = lib.Celsius(3)     //@NC09
	_ = gen.List[int](gen.List[int]{}) //@NC10
	_ = func() string(localHello)  //@NC11
	s := []int{}
	_ = len(s)              //@BI01
	s = append(s, 1)        //@BI02
	m := make(map[string]int) //@BI03
	q := new(int)           //@BI04
	_ = min(1, 2)           //@BI05
	_ = max(1, 2)           //@BI06
	clear(m)                //@BI07
	defer func() { recover() }() //@BI08
	if q == nil {
		panic("x") //@BI09
	}
	_ = unsafe.Sizeof(x)    //@BI10
	_ = initVal
}
