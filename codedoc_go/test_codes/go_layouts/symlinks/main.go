package main

import (
	"fmt"

	ka "example.com/layouts/symlinks/a"
	kb "example.com/layouts/symlinks/b"
)

func main() {
	fmt.Println(ka.Describe(), kb.Describe())
}
