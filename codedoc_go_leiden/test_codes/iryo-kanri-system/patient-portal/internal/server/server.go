package server

import (
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"

	"example.com/iryo/billing"
	"example.com/iryo/domain/patient"
	"example.com/iryo/kernel/notify"
	"example.com/iryo/scheduling"
)

type Portal struct {
	book *scheduling.AppointmentBook
	fees *billing.FeeCalculator
}

func NewPortal() *Portal {
	n := notify.MailNotifier{To: "portal@iryo.example"}
	return &Portal{book: scheduling.NewAppointmentBook(n), fees: billing.NewFeeCalculator(10)}
}

func (p *Portal) Routes() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("/fees", p.handleFees)
	mux.Handle("/health", http.HandlerFunc(p.handleHealth))
	mux.HandleFunc("/appointments", p.handleAppointments)
	return mux
}

func (p *Portal) handleFees(w http.ResponseWriter, r *http.Request) {
	points := 120
	fmt.Fprintf(w, "診察料 %d円", p.fees.Calculate(points, 1))
}

func (p *Portal) handleHealth(w http.ResponseWriter, r *http.Request) {
	io.WriteString(w, "ok")
}

func (p *Portal) handleAppointments(w http.ResponseWriter, r *http.Request) {
	name := r.URL.Query().Get("name")
	slot := p.book.Reserve(10, name)
	fmt.Fprintf(w, "予約 %d時 %s", slot.Hour, patient.Patient{Name: slot.Name}.Describe())
}

func Demo(out io.Writer) {
	fmt.Fprintln(out, "=== 患者ポータルデモ開始 ===")
	p := NewPortal()
	mux := p.Routes()
	for _, path := range []string{"/fees", "/health", "/appointments?name=佐藤"} {
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
		fmt.Fprintf(out, "%s -> %d %s\n", path, rec.Code, strings.TrimSpace(rec.Body.String()))
	}
	fmt.Fprintln(out, "=== 患者ポータルデモ完了 ===")
}
