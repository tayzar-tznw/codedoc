//go:build ignore

package main

import "fmt"

func Helper() string {
	return "gen"
}

func main() {
	fmt.Println(Helper())
}
