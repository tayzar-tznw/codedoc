package x

import "os"

func a() {}
func b() {}

func Stop(exit bool) {
	defer a()
	if exit {
		defer b()
		os.Exit(0)
	}
}
