package report

import "example.com/iryo/shared/money"

func Summary(yen int64) string {
	return money.NewPrice(yen).Format()
}
