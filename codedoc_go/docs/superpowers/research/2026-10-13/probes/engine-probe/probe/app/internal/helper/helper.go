package helper

import "example.com/lib/trace"

func Help() string {
	trace.Hit()
	return "help"
}
