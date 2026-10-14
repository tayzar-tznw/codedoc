package textkit

import "strings"

func Banner(s string) string {
	return "== " + strings.ToUpper(s) + " =="
}

func Frame(s string) string {
	return "[" + Banner(s) + "]"
}
