package clock

import (
	"time"

	"example.com/iryo/shared/contracts"
)

type SystemClock struct{}

func (SystemClock) Now() time.Time {
	now := time.Now()
	return now
}

func Default() contracts.Clock {
	return SystemClock{}
}

func Stamp(c contracts.Clock) string {
	return c.Now().Format("2006-01-02")
}
