package checks

import (
	"example.com/iryo/textkit"
	"example.com/thirdparty/uuidlite"
)

type Logger struct {
	lines []string
}

func (l *Logger) Info(msg string) {
	l.lines = append(l.lines, msg)
}

func Run() []string {
	l := &Logger{}
	l.Info(textkit.Banner("checks"))
	l.Info(uuidlite.FromInt(42).String())
	return l.lines
}
