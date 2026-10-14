package linedir

import "strings"

//line view.tmpl:1
func Render(name string) string {
	return strings.ToUpper(name) + suffix()
}

//line view.tmpl:30
func suffix() string {
	return /*line view.tmpl:40:9*/ mark("!")
}

func mark(s string) string {
	return s
}
