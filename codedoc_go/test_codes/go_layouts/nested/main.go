package main

import (
	"fmt"

	"example.com/layouts/nested/inner/calc"
	"example.com/layouts/nested/report"
)

func main() {
	fmt.Println(report.Line(calc.Twice(21)))
}
