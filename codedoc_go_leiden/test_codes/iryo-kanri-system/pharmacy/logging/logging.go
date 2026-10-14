package logging

import "fmt"

type Logger struct {
	station string
	lines   []string
}

func New(station string) *Logger {
	return &Logger{station: station}
}

func (l *Logger) Log(msg string) {
	l.lines = append(l.lines, fmt.Sprintf("[調剤] %s: %s", l.station, msg))
}

func (l *Logger) Lines() []string {
	return l.lines
}
