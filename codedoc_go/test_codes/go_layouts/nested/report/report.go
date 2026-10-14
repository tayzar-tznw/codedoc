package report

import "strconv"

func Line(n int) string {
	return "total=" + strconv.Itoa(Twice(n))
}

func Twice(n int) int {
	return n + n
}
