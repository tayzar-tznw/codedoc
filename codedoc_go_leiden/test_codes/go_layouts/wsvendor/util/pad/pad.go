package pad

import "strings"

func Left(s string, width int) string {
	return strings.Repeat(" ", gap(len(s), width)) + s
}

func gap(n, width int) int {
	if n >= width {
		return 0
	}
	return width - n
}
