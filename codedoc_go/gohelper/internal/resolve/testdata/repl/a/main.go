package main

import "example.com/b/lib"

type Shape interface{ Area() float64 }

func Measure(s Shape) float64 { return s.Area() }

func main() { _ = Measure(lib.Square{}) }
