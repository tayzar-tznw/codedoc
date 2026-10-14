package main

import (
	"fmt"
	"strings"
)

func main() {
	fmt.Println(banner("present"))
}

func banner(s string) string {
	line := strings.Repeat("=", 3)
	return line + " " + s
}
