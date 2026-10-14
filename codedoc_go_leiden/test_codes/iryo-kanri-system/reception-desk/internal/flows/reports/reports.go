package reports

import (
	"fmt"
	"io"
	"strings"

	"example.com/iryo/data/cache"
	"example.com/iryo/data/store"
	"example.com/iryo/domain/patient"
	"example.com/iryo/domain/wards/north"
	"example.com/iryo/domain/wards/south"
	"example.com/iryo/kernel/logging"
	"example.com/iryo/kernel/policy"
	"example.com/iryo/reporting"
	"example.com/iryo/scheduling"
)

type PatientList struct {
	store.MemoryStore[patient.Patient]
	Title string
}

func countAll(s *store.Store[string]) int {
	return s.Count()
}

func label(s string) string {
	return "《" + s + "》"
}

func labelsOf(items []any) []string {
	type labeler interface{ Label() string }
	var out []string
	for _, it := range items {
		if l, ok := it.(labeler); ok {
			out = append(out, l.Label())
		}
	}
	return out
}

func Run(w io.Writer) {
	fmt.Fprintln(w, "--- 帳票 ---")
	pt := patient.Patient{Name: "佐藤"}
	s := store.NewMemoryStore[patient.Patient]()
	s.Put(1, pt)
	got, ok := s.Get(1)
	fmt.Fprintln(w, got.Name, ok, s.IDs())
	names := store.NewMemoryStore[string]()
	names.Put(1, "佐藤")
	names.Put(2, "鈴木")
	fmt.Fprintln(w, countAll(names), store.FirstOf([]string{"初診", "再診"}))
	fmt.Fprintln(w, store.Sum([]int{288, 73, 225}))
	fmt.Fprintln(w, store.Map([]string{"a", "b"}, strings.ToUpper), store.Map([]string{"診察", "処方"}, label))
	charts := cache.New[string, int]()
	charts.Put("血圧", 120)
	bp, _ := charts.Get("血圧")
	fmt.Fprintln(w, bp, charts.Len())
	inpatients := []patient.Inpatient{{Patient: patient.Patient{Name: "田中"}, Ward: "西2"}}
	fmt.Fprintln(w, patient.DescribeAll(inpatients))
	list := PatientList{MemoryStore: *store.NewMemoryStore[patient.Patient](), Title: "外来"}
	list.Put(7, pt)
	fmt.Fprintln(w, list.Title, list.Count())
	book := scheduling.NewAppointmentBook(nil)
	morning := book.Reserve(9, "佐藤")
	noon := book.Reserve(10, "鈴木")
	fmt.Fprintln(w, reporting.LabelAll([]scheduling.Slot{morning, noon}))
	fmt.Fprintln(w, labelsOf([]any{policy.UrgentPolicy{}, morning, "ラベルなし"}))
	for slot := range book.Each {
		fmt.Fprintln(w, "予約", slot.Hour, slot.Name)
	}
	for slot := range book.All() {
		fmt.Fprintln(w, "一覧", slot.Name)
	}
	for n := range scheduling.Countdown {
		fmt.Fprintln(w, "残り", n)
	}
	nw := north.Ward{Name: "北病棟"}
	sw := south.Ward{Name: "南病棟"}
	fmt.Fprintln(w, nw.Name, nw.Beds(), sw.Name, sw.Beds())
	sink := &reporting.MemorySink{}
	trail := reporting.AuditTrail{Sink: sink, Name: "監査"}
	trail.Note("帳票出力")
	reporting.Publish(trail, "月報")
	fmt.Fprintln(w, strings.Join(sink.Lines, " / "))
	monthly := reporting.NewMonthlyReport("2026-10", logging.New("帳票"))
	fmt.Fprintln(w, monthly.Render([]patient.Patient{pt}))
}
