package main

import (
	"fmt"

	"example.com/iryo/shared/log"
	"example.com/iryo/shared/money"
)

func main() {
	p := money.NewPrice(980).Add(money.NewPrice(20))
	log.New("legacy").Info(money.Legacy(money.Round(p.Amount())))
	fmt.Println("legacy done")
}
