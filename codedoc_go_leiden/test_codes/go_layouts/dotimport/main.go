package main

import (
	"fmt"
	. "strings"

	"example.com/layouts/dotimport/other"
	. "example.com/layouts/dotimport/strutil"
)

func main() {
	fmt.Println(Reverse(ToUpper("dot")), other.Reverse("plain"))
}
