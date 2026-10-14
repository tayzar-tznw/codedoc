package scheduling

import (
	"iter"
	"strconv"

	"example.com/iryo/domain/patient"
	"example.com/iryo/kernel/notify"
)

type Slot struct {
	Hour int
	Name string
}

func (s Slot) Label() string {
	return strconv.Itoa(s.Hour) + "時 " + s.Name
}

type AppointmentBook struct {
	slots    []Slot
	notifier notify.Notifier
}

func NewAppointmentBook(n notify.Notifier) *AppointmentBook {
	return &AppointmentBook{notifier: n}
}

func (b *AppointmentBook) Reserve(hour int, name string) Slot {
	s := Slot{Hour: hour, Name: name}
	b.slots = append(b.slots, s)
	return s
}

func (b *AppointmentBook) ReserveFor(p patient.Patient, hour int) string {
	s := b.Reserve(hour, p.Name)
	return b.notifier.Send(s.Name + " 様の予約")
}

func (b *AppointmentBook) All() iter.Seq[Slot] {
	return func(yield func(Slot) bool) {
		for _, s := range b.slots {
			if !yield(s) {
				return
			}
		}
	}
}

func (b *AppointmentBook) Each(yield func(Slot) bool) {
	for _, s := range b.slots {
		if !yield(s) {
			return
		}
	}
}

func Countdown(yield func(int) bool) {
	for n := 3; n > 0; n-- {
		if !yield(n) {
			return
		}
	}
}
