package orders

import (
	"example.com/iryo/shared/log"
	"example.com/iryo/shared/money"
)

type OrderService struct {
	*log.Logger
	taxRate int64
}

func NewOrderService(rate int64) *OrderService {
	return &OrderService{Logger: log.New("注文"), taxRate: rate}
}

func (s *OrderService) Total(baseYen int64) int64 {
	base := money.NewPrice(baseYen)
	tax := money.NewPrice(baseYen * s.taxRate / 100)
	sum := base.Add(tax)
	s.Info("合計 " + sum.Format())
	return money.Round(sum.Amount())
}
