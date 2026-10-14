package main

import (
	"fmt"

	"example.com/iryo/api/service/billing"
	"example.com/iryo/shared/money"
)

func main() {
	svc := billing.NewInvoiceService()
	fmt.Println(svc.Charge(money.NewPrice(5000)))
	fmt.Println(svc.Record("監査", money.NewPrice(1200)).Stamp())
}
