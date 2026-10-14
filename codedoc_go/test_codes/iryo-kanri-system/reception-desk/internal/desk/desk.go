package desk

import (
	"fmt"
	"io"

	"example.com/iryo/reception-desk/internal/flows/accounting"
	"example.com/iryo/reception-desk/internal/flows/checkin"
	"example.com/iryo/reception-desk/internal/flows/closing"
	"example.com/iryo/reception-desk/internal/flows/dispatch"
	"example.com/iryo/reception-desk/internal/flows/maintenance"
	"example.com/iryo/reception-desk/internal/flows/pharmacyflow"
	"example.com/iryo/reception-desk/internal/flows/reports"
	"example.com/iryo/reception-desk/internal/textreport"
)

func Run(w io.Writer) {
	fmt.Fprintln(w, "=== 受付業務デモ開始 ===")
	checkin.Run(w)
	accounting.Run(w)
	pharmacyflow.Run(w)
	closing.Run(w)
	dispatch.Run(w)
	reports.Run(w)
	maintenance.Run(w)
	fmt.Fprintln(w, textreport.Heading("daily"))
	fmt.Fprint(w, textreport.Render("daily summary", [][2]string{{"初診", "288"}, {"再診", "73"}}))
	fmt.Fprintln(w, textreport.Line("合計", 361))
	fmt.Fprintln(w, "=== 受付業務デモ完了 ===")
}
