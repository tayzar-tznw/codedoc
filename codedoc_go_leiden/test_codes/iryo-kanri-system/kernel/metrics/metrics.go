package metrics

import (
	_ "unsafe"

	_ "example.com/iryo/kernel/internal/tally"
)

//go:linkname bump example.com/iryo/kernel/internal/tally.bump
func bump(counter string, n int) int

//go:linkname nanotime runtime.nanotime
func nanotime() int64

func Count(counter string, n int) int {
	return bump(counter, n)
}

func Started() int64 {
	return nanotime()
}

func Elapsed(start int64) bool {
	return nanotime() >= start
}
