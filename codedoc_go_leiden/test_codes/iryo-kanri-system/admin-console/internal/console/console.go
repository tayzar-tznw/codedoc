package console

import (
	"fmt"
	"io"

	"example.com/iryo/admin-console/internal/batch"
	"example.com/iryo/admin-console/internal/textreport"
	"example.com/iryo/billing"
	"example.com/iryo/domain/patient"
	"example.com/iryo/kernel/audit"
	"example.com/iryo/kernel/guard"
	"example.com/iryo/kernel/logging"
	"example.com/iryo/kernel/notify"
	"example.com/iryo/reporting"
)

func Run(w io.Writer) {
	fmt.Fprintln(w, "=== 管理コンソール開始 ===")
	batch.Run(w)
	if err := guard.Require(w, "writer"); err != nil {
		fmt.Fprintln(w, err)
	}
	calc := billing.NewFeeCalculator(10)
	fmt.Fprintln(w, calc.Calculate(288, 1))
	monthly := reporting.NewMonthlyReport("2026-09", logging.New("管理"))
	fmt.Fprintln(w, monthly.Render([]patient.Patient{{Name: "伊藤"}}))
	var n notify.Notifier = notify.MailNotifier{To: "admin@iryo.example"}
	fmt.Fprintln(w, n.Send("月次締め完了"))
	auditLog := &audit.ConsoleLogger{Prefix: "管理: "}
	auditLog.Printf("月次 %d件", 12)
	fmt.Fprintln(w, auditLog.Lines[0])
	fmt.Fprintln(w, textreport.Heading("monthly"))
	fmt.Fprint(w, textreport.Render("monthly summary", [][2]string{{"初診", "288"}, {"再診", "73"}}))
	fmt.Fprintln(w, textreport.Line("合計", 361))
	fmt.Fprintln(w, "=== 管理タスク完了 ===")
}
