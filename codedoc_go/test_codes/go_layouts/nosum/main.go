package main

import (
	"fmt"

	"example.com/thirdparty/uuidlite"
)

func main() {
	fmt.Println(uuidlite.FromInt(7).String(), tag())
}

func tag() string {
	return "nosum"
}
