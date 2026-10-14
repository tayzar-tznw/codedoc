package s

import "strings"

func boom() int            { panic("boom") }
func keep(n int, s string) {}

func Pair() (ok bool) {
	defer func() { ok = recover() != nil }()
	keep(boom(), strings.ToUpper("x"))
	return false
}
