package inv

import "example.com/missing/gauge"

type Manager struct{}

type Ready interface {
	Get() (*Manager, error)
}

type maxTS struct {
	value float64
	gauge.Gauge
}

func (m *maxTS) Get() float64 { return m.value }

type Good struct{}

func (Good) Get() (*Manager, error) { return nil, nil }

type Mixed struct{ gauge.Gauge }

func (Mixed) Get() (*Manager, error) { return nil, nil }

type PtrGood struct{ gauge.Gauge }

func (*PtrGood) Get() (*Manager, error) { return nil, nil }

type maxV struct {
	value float64
	gauge.Gauge
}

func (m maxV) Get() float64 { return m.value }
