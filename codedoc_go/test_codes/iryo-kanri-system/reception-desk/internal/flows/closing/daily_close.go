package closing

import (
	"fmt"
	"io"

	"example.com/iryo/billing/pointmath"
	"example.com/iryo/billing/validation"
	"example.com/iryo/kernel/logging"
)

func Run(w io.Writer) {
	fmt.Fprintln(w, "--- 日次締め (受付) ---")
	ledger := logging.New("日次点検")
	ledger.Log("日次点検")
	check := validation.Validator{}
	ok := check.Validate("H12345678")
	yen := pointmath.ToYen(15)
	for _, line := range ledger.Lines() {
		fmt.Fprintln(w, line)
	}
	fmt.Fprintf(w, "検証=%v 金額=%d円\n", ok, yen)
}
