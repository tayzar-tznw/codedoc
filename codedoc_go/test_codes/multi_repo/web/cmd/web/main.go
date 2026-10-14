package main

import (
	"fmt"

	"example.com/iryo/web/checks"
	"example.com/iryo/web/clock"
	"example.com/iryo/web/orders"
)

func main() {
	svc := orders.NewOrderService(10)
	fmt.Println(svc.Total(1234))
	fmt.Println(len(clock.Stamp(clock.Default())))
	fmt.Println(checks.Run())
}
