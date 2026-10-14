package lib

import "unsafe"

func cleanup()     {}
func cleanupLoop() {}
func cleanupPtr()  {}
func compute() int { return 1 }

func Recovered() {
	defer cleanup()
	defer func() { recover() }()
	panic(compute())
}

func Looping() {
	defer cleanupLoop()
	defer func() { recover() }()
	for {
		_ = Crash()
	}
}

func Crash() int {
	var m map[string]int
	m["x"] = 1
	return 0
}

var FP = new(func() int)

func PtrDefer() int {
	defer cleanupPtr()
	*FP = compute
	return (*FP)()
}

func Size() uintptr { return unsafe.Sizeof(compute()) }
