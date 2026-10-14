package billing

import (
	"example.com/iryo/api/notify/mail"
	"example.com/iryo/shared/contracts"
	"example.com/iryo/shared/log"
	"example.com/iryo/shared/money"
	"example.com/iryo/textkit"
)

type InvoiceService struct {
	logger *log.Logger
}

type AuditedInvoice struct {
	contracts.Audit
	Total money.Price
}

func NewInvoiceService() *InvoiceService {
	return &InvoiceService{logger: log.New("請求")}
}

func (s *InvoiceService) Charge(amount money.Price) string {
	label := amount.Format()
	s.logger.Info("請求 " + label)
	return mail.Compose(textkit.Banner(label))
}

func (s *InvoiceService) Record(by string, total money.Price) AuditedInvoice {
	inv := AuditedInvoice{Audit: contracts.Audit{By: by}, Total: total}
	s.logger.Info(inv.Stamp())
	return inv
}
