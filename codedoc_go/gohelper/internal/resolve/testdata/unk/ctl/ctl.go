package ctl

import (
	"example.com/missing/gauge"
	"example.com/unk/api"
)

type Tracker struct{ gauge.Gauge }

func (Tracker) Get() float64 { return 0 }

type Good struct{}

func (Good) Get() (*api.Manager, error) { return nil, nil }
