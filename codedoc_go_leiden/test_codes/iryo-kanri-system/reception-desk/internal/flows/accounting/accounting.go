package accounting

import (
	"fmt"
	"io"

	"example.com/iryo/billing"
	btext "example.com/iryo/billing/text"
	"example.com/iryo/domain/money"
	itext "example.com/iryo/interop/text"
	"example.com/iryo/kernel/policy"
	"example.com/iryo/legacy-claims"
)

func charge(p policy.PaymentPolicy, amount int) int {
	return p.Fee(amount)
}

func settle(w io.Writer) {
	sess := billing.Open("会計窓口")
	defer report(w, sess)
	sess.Charge(1200)
	sess.Charge(340)
}

func report(w io.Writer, s *billing.Session) {
	fmt.Fprintln(w, s.Close())
}

func Run(w io.Writer) {
	fmt.Fprintln(w, "--- 会計 ---")
	standard := policy.StandardPolicy{BasePolicy: policy.BasePolicy{Rate: 5}}
	urgent := policy.UrgentPolicy{BasePolicy: policy.BasePolicy{Rate: 5}}
	fmt.Fprintln(w, standard.Fee(10000), urgent.Fee(10000), urgent.Label())
	fmt.Fprintln(w, urgent.BasePolicy.Fee(10000))
	fmt.Fprintln(w, charge(standard, 2000), charge(urgent, 2000))
	consultation := money.Yen(2880)
	medication := money.Points(146).ToYen()
	total := consultation.Add(medication)
	current := btext.ReceiptFormatter{}
	legacy := rezept.ReceiptFormatter{}
	bridge := itext.ReceiptFormatter{}
	fmt.Fprintln(w, current.Format(int(total)))
	fmt.Fprintln(w, legacy.Format(int(total)))
	fmt.Fprintln(w, bridge.Format(int(total)))
	fmt.Fprintln(w, total)
	calc := billing.NewFeeCalculator(10)
	fmt.Fprintln(w, calc.Scaled(3), billing.Round(1234))
	fmt.Fprintln(w, (*billing.FeeCalculator).Calculate(calc, 2, 3))
	settle(w)
}
