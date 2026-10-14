package main

import (
	"os"

	"example.com/iryo/reception-desk/internal/desk"
)

func main() {
	desk.Run(os.Stdout)
}
