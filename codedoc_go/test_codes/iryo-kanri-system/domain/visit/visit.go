package visit

import (
	"example.com/iryo/domain/patient"
	"example.com/iryo/kernel/clock"
)

type Visit struct {
	*patient.Patient
	Reason string
}

func (v Visit) Summary() string {
	return v.Describe() + " / " + v.Reason
}

func (v Visit) Dated(c clock.Clock) string {
	return clock.Stamp(c) + " " + v.Reason
}
