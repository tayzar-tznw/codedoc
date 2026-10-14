package main

import (
	"os"

	"example.com/iryo/patient-portal/internal/server"
)

func main() {
	server.Demo(os.Stdout)
}
