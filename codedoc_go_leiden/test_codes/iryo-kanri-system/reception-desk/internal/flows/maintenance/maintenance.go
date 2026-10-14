package maintenance

import (
	"errors"
	"fmt"
	"io"
	"log"
	"reflect"
	"sort"
	"strings"
	"sync"
	"time"
	"unsafe"

	"example.com/iryo/billing/tariff"
	"example.com/iryo/domain/patient"
	"example.com/iryo/interop/exporters"
	"example.com/iryo/interop/idkit"
	"example.com/iryo/interop/lookup"
	"example.com/iryo/interop/records"
	"example.com/iryo/kernel/audit"
	"example.com/iryo/kernel/escalation"
	"example.com/iryo/kernel/fastmath"
	"example.com/iryo/kernel/metrics"
	"example.com/iryo/kernel/platform"
	"example.com/iryo/legacy-claims"
)

type wardLock struct {
	sync.Mutex
	beds int
}

func Run(w io.Writer) {
	fmt.Fprintln(w, "--- 保守 ---")
	start := metrics.Started()
	fmt.Fprintln(w, platform.Describe(), fastmath.Total([]int64{288, 73}))
	fmt.Fprintln(w, metrics.Count("受付", 2), metrics.Elapsed(start))
	audit.Record(log.New(w, "", 0), "保守", 2)
	fmt.Fprintln(w, rezept.CodeLabel("a001"), rezept.KnownCodes())
	ids := idkit.NewPatientIDs(100)
	fmt.Fprintln(w, ids.Issue(), ids.Issue())
	if canon, err := idkit.Canonical("00000000-0000-4000-8000-00000000002a"); err == nil {
		fmt.Fprintln(w, canon)
	}
	if table, err := tariff.Load(); err == nil {
		fmt.Fprintln(w, table.Points("A000"), table.Codes())
	}
	exps := []exporters.Exporter{exporters.CsvExporter{}, exporters.XmlExporter{Root: "rows"}}
	fmt.Fprintln(w, exporters.ExportAll(exps, []string{"A000", "B001"}))
	if csv, ok := exps[0].(exporters.CsvExporter); ok {
		fmt.Fprintln(w, csv.Export([]string{"直接"}))
	}
	fmt.Fprintln(w, exporters.ArchiveIfAny(nil, 2026) == nil)
	fmt.Fprintln(w, records.KindOf(records.Claim{No: "C-1"}), records.KindOf(records.Refund{No: "R-1"}))
	tracker := idkit.NewTracker(0.75)
	fmt.Fprintln(w, lookup.Resolve(lookup.Fixed{Value: "W-7"}), tracker.Get())
	fmt.Fprintln(w, escalation.Evaluate(escalation.Policy{Ward: "西"}))
	lock := &wardLock{beds: 4}
	lock.Lock()
	lock.beds--
	lock.Unlock()
	codes := []string{"B001", "A000", "A001"}
	sort.Slice(codes, func(i, j int) bool {
		return codes[i] < codes[j]
	})
	var sb strings.Builder
	sb.WriteString(strings.Join(codes, ","))
	day := time.Date(2026, 10, 13, 0, 0, 0, 0, time.UTC)
	done := errors.New("保守完了")
	fmt.Fprintln(w, sb.String(), day.Format("2006-01-02"), lock.beds, done)
	fmt.Fprintln(w, done.Error())
	pt := patient.Patient{Name: "高橋"}
	size := unsafe.Sizeof(pt)
	shortest := min(len(codes), 2)
	counts := map[string]int{"A000": 1}
	clear(counts)
	visits := new(len(codes) * 2)
	fmt.Fprintln(w, size > 0, shortest, len(counts), *visits)
	method := reflect.ValueOf(pt).MethodByName("Describe")
	fmt.Fprintln(w, method.Call(nil)[0].String())
}
