package main

import (
	"fmt"

	"example.com/thirdparty/newgo"
)

func main() {
	fmt.Println(newgo.F(), local())
}

func local() int {
	return 2
}
