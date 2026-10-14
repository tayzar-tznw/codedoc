package main

import (
	"os"

	"example.com/iryo/admin-console/internal/console"
)

func main() {
	console.Run(os.Stdout)
}
