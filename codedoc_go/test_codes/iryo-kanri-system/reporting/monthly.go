package reporting

import (
	"fmt"

	"example.com/iryo/domain/patient"
	"example.com/iryo/kernel/logging"
)

type MonthlyReport struct {
	Month string
	log   *logging.Logger
}

func NewMonthlyReport(month string, log *logging.Logger) *MonthlyReport {
	return &MonthlyReport{Month: month, log: log}
}

func (m *MonthlyReport) Render(visits []patient.Patient) string {
	m.log.Log("月報 " + m.Month)
	return fmt.Sprintf("%s 受診 %d件 先頭=%s", m.Month, len(visits), first(visits))
}

func first(visits []patient.Patient) string {
	if len(visits) == 0 {
		return "-"
	}
	return visits[0].Describe()
}
