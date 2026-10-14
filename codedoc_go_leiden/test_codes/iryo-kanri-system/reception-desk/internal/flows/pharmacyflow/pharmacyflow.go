package pharmacyflow

import (
	"fmt"
	"io"
	"strings"

	"example.com/iryo/billing"
	billingval "example.com/iryo/billing/validation"
	klog "example.com/iryo/kernel/logging"
	rezept "example.com/iryo/legacy-claims"
	"example.com/iryo/pharmacy"
	plog "example.com/iryo/pharmacy/logging"
	rxval "example.com/iryo/pharmacy/validation"
)

func Run(w io.Writer) {
	fmt.Fprintln(w, "--- 薬局窓口 ---")
	houseLog := klog.New("薬局窓口")
	dispenseLog := plog.New("調剤室")
	houseLog.Log("処方箋受領")
	dispenseLog.Log("処方箋受領")
	rx := rxval.Validator{}
	insurance := billingval.Validator{}
	fmt.Fprintln(w, rx.Validate("Rp アモキシシリン 250mg"), insurance.Validate("H12345678"))
	dose := pharmacy.DoseCalculator{}
	fee := billing.NewFeeCalculator(10)
	table := rezept.PointTable{}
	fmt.Fprintln(w, dose.Calculate(30, 10), fee.Calculate(30, 10), table.Calculate(30, 10))
	labels := pharmacy.StampAll(pharmacy.RubberStamp{Name: "薬剤部"}, []string{"確認", "払出"})
	fmt.Fprintln(w, strings.Join(labels, " "))
	for _, line := range append(houseLog.Lines(), dispenseLog.Lines()...) {
		fmt.Fprintln(w, line)
	}
}
