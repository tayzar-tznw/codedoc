package dispatch

import (
	"fmt"
	"io"
	"strings"
	"sync"

	"example.com/iryo/billing"
	"example.com/iryo/interop/records"
	"example.com/iryo/kernel/logging"
	"example.com/iryo/kernel/registry"
	"example.com/iryo/pharmacy"
	"example.com/iryo/reporting"
)

type Step func(int) int

type Route struct {
	Name   string
	Handle func(string) string
}

type Checker struct {
	Validate func(id string) bool
}

var Hooks = map[string]func(int) int{
	"tax":   billing.AddTax,
	"round": billing.Round,
}

var Fallback = billing.Round

func looksInsured(id string) bool {
	return strings.HasPrefix(id, "H")
}

func apply(fn func(int) int, yen int) int {
	return fn(yen)
}

func makeAdder(base int) func(int) int {
	return func(n int) int {
		return base + n
	}
}

func worker(wg *sync.WaitGroup, out []string, i int) {
	defer wg.Done()
	out[i] = fmt.Sprintf("worker%d=%d", i, billing.Round(1000+i*7))
}

func recovered() (out string) {
	defer func() {
		if r := recover(); r != nil {
			out = fmt.Sprint("回復: ", r)
		}
	}()
	billing.NewFeeCalculator(10).Calculate(-1, 1)
	return "到達しない"
}

func tally(w io.Writer) (n int) {
	defer func() { fmt.Fprintln(w, "集計", n) }()
	n = len(registry.Names())
	return n
}

func Run(w io.Writer) {
	fmt.Fprintln(w, "--- 振り分け ---")
	log := logging.New("振り分け")

	round := billing.Round
	fmt.Fprintln(w, round(1234))

	logf := log.Log
	logf("別名呼び出し")

	double := func(n int) int {
		return n * 2
	}
	fmt.Fprintln(w, double(21))

	pick := billing.Round
	if len(log.Lines()) > 0 {
		pick = billing.AddTax
	}
	fmt.Fprintln(w, pick(500))

	keep := billing.Round
	ref := &keep
	fmt.Fprintln(w, keep(777), *ref != nil)

	late := billing.Round
	func() {
		late = billing.AddTax
	}()
	fmt.Fprintln(w, late(800))

	fmt.Fprintln(w, apply(billing.AddTax, 1000))
	fmt.Fprintln(w, Hooks["tax"](2000))
	fmt.Fprintln(w, Fallback(1999))

	steps := []Step{Step(double), billing.Round}
	for i := range steps {
		fmt.Fprintln(w, steps[i](600))
	}

	route := Route{Name: "upper", Handle: strings.ToUpper}
	fmt.Fprintln(w, route.Handle("route"))
	checker := Checker{Validate: looksInsured}
	fmt.Fprintln(w, checker.Validate("H12345678"))

	add := makeAdder(2)
	fmt.Fprintln(w, add(3))

	trail := reporting.AuditTrail{Sink: &reporting.MemorySink{}, Name: "振り分け"}
	write := trail.Write
	fmt.Fprintln(w, write("別名経由の記録"))
	stamp := pharmacy.RubberStamp.Stamp
	fmt.Fprintln(w, stamp(pharmacy.RubberStamp{Name: "受付"}, "控え"))
	kind := records.Record.Kind
	fmt.Fprintln(w, kind(records.Refund{No: "R-2"}))

	fmt.Fprintln(w, registry.Call("echo", "やまびこ"), registry.Call("upper", "abc"))
	fmt.Fprintln(w, registry.Normalize("  空白  "), registry.Banner(), registry.Names())

	var wg sync.WaitGroup
	results := make([]string, 2)
	wg.Add(2)
	go func() {
		defer wg.Done()
		results[0] = fmt.Sprintf("closure=%d", billing.AddTax(100))
	}()
	go worker(&wg, results, 1)
	wg.Wait()
	fmt.Fprintln(w, strings.Join(results, " "))

	fmt.Fprintln(w, func() string {
		return "即時実行"
	}())

	fmt.Fprintln(w, recovered(), tally(w))
	for _, line := range log.Lines() {
		fmt.Fprintln(w, line)
	}
}
