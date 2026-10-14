package clock

import (
	"strings"
	"time"
)

type Clock interface {
	Now() time.Time
}

type OfficeClock struct {
	Day time.Time
}

func (c OfficeClock) Now() time.Time {
	return c.Day
}

func Stamp(c Clock) string {
	return c.Now().Format("2006-01-02")
}

func normalize(label string) string {
	return strings.TrimSpace(label)
}

func Label(c Clock, label string) string {
	return normalize(label) + "@" + Stamp(c)
}
