package m

import "regexp"

func use(n int) int { return n }

func MustFail() (ok bool) {
	defer func() { ok = recover() != nil }()
	regexp.MustCompile("(")
	use(2)
	return false
}
