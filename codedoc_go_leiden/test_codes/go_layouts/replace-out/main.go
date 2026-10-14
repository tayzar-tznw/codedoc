package main

import (
	"fmt"

	"example.com/outside/extlib"
)

func main() {
	fmt.Println(describe(extlib.NewLabel(120)))
}

func describe(l extlib.Label) string {
	return "label " + l.Text()
}
