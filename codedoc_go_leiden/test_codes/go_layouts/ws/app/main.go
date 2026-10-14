package main

import (
	"fmt"

	"example.com/layouts/ws/lib/tally"
)

func main() {
	c := tally.New()
	c.Add(2)
	fmt.Println(tally.Sum(c.Total(), 3), tally.Label(c))
	fmt.Print(headline())
}
