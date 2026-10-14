package idkit

import "example.com/thirdparty/uuidlite"

type Tracker struct {
	uuidlite.Sequence
	load float64
}

func NewTracker(load float64) *Tracker {
	return &Tracker{load: load}
}

func (p *Tracker) Get() float64 {
	return p.load
}
