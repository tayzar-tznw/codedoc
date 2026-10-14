package main

import (
	"fmt"

	"example.com/layouts/linedir"
)

func main() {
	fmt.Println(linedir.Render("go"))
	fmt.Println(linedir.CallLined())
}
