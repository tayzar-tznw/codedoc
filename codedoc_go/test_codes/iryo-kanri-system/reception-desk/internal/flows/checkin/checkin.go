package checkin

import (
	"fmt"
	"io"
	"time"

	"example.com/iryo/data/repository"
	"example.com/iryo/domain/patient"
	"example.com/iryo/domain/visit"
	"example.com/iryo/kernel/clock"
	"example.com/iryo/kernel/logging"
	"example.com/iryo/kernel/notify"
	"example.com/iryo/scheduling"
)

func find(repo repository.Repository[patient.Patient], id int) (patient.Patient, error) {
	return repo.Find(id)
}

func describeAny(v any) string {
	return v.(interface{ Describe() string }).Describe()
}

func Run(w io.Writer) {
	fmt.Fprintln(w, "--- 受付 ---")
	log := logging.New("受付")
	repo := repository.NewSqlPatientRepository(log)
	ae := patient.AuditedEntity{Entity: patient.Entity{ID: 1}}
	pt := patient.Patient{AuditedEntity: patient.AuditedEntity{Entity: patient.Entity{ID: 10}}, Name: "山田"}
	ip := patient.Inpatient{Patient: pt, Ward: "東3"}
	fmt.Fprintln(w, ae.Touch(), pt.Touch(), ip.Touch())
	fmt.Fprintln(w, pt.Describe(), ip.Describe())
	fmt.Fprintln(w, ip.Patient.AuditedEntity.Entity.Describe())
	fmt.Fprintln(w, ip.Record("入院"), pt.Save())
	v := visit.Visit{Patient: &pt, Reason: "発熱"}
	fmt.Fprintln(w, v.Summary(), v.Touch())
	day := clock.OfficeClock{Day: time.Date(2026, 10, 13, 9, 0, 0, 0, time.UTC)}
	fmt.Fprintln(w, v.Dated(day))
	if err := repo.Save(pt); err != nil {
		fmt.Fprintln(w, "保存失敗:", err)
	}
	found, err := find(repo, pt.ID)
	if err != nil {
		fmt.Fprintln(w, "検索失敗:", err)
	}
	fmt.Fprintln(w, describeAny(found))
	sms := &scheduling.SmsNotifier{Number: "090-0000-0000"}
	notifiers := []notify.Notifier{notify.MailNotifier{To: "desk@iryo.example"}, sms}
	for _, line := range notify.Broadcast(notifiers, "受付完了") {
		fmt.Fprintln(w, line)
	}
	fmt.Fprintln(w, notify.Relay(sms, "再送"))
	send := notifiers[0].Send
	fmt.Fprintln(w, send("呼出"))
	fmt.Fprintln(w, notify.Notifier.Send(notify.MailNotifier{To: "ward@iryo.example"}, "病棟連絡"))
	book := scheduling.NewAppointmentBook(sms)
	fmt.Fprintln(w, book.ReserveFor(pt, 11))
	for _, line := range log.Lines() {
		fmt.Fprintln(w, line)
	}
}
