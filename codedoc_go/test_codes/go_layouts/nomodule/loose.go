package main

import "fmt"

func main() {
	fmt.Println(greet("loose"))
}

func greet(s string) string {
	return "hi " + s
}
