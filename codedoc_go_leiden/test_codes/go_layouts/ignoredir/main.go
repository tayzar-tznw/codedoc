package main

import (
	"fmt"

	"example.com/layouts/ignoredir/generated"
)

func main() {
	fmt.Println(generated.Banner(), local())
}

func local() string {
	return "local"
}
