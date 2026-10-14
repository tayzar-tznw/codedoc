package pharmacy

import "example.com/iryo/kernel/guard"

type DoseCalculator struct{}

func (DoseCalculator) Calculate(points, count int) int {
	return guard.EnsurePositive(points) * count
}

type Stamper interface {
	Stamp(label string) string
}

type RubberStamp struct {
	Name string
}

func (r RubberStamp) Stamp(label string) string {
	return "[" + r.Name + "] " + label
}

func StampAll(s Stamper, labels []string) []string {
	out := make([]string, 0, len(labels))
	for _, l := range labels {
		out = append(out, s.Stamp(l))
	}
	return out
}
