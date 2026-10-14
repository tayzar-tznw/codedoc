package main

import (
	"fmt"

	"example.com/layouts/skipdirs/.cache"
	draft "example.com/layouts/skipdirs/_draft"
	"example.com/layouts/skipdirs/testdata/golden"
)

func main() {
	fmt.Println(draft.Sketch(), golden.Load(), cache.Get(), local())
}

func local() string {
	return "local"
}
