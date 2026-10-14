package logging

import "strings"

type Logger struct {
	prefix string
	lines  []string
}

func New(prefix string) *Logger {
	return &Logger{prefix: prefix}
}

func (l *Logger) Log(msg string) {
	l.lines = append(l.lines, "[記録] "+l.prefix+": "+msg)
}

func (l *Logger) Lines() []string {
	return l.lines
}

func (l *Logger) Dump() string {
	return strings.Join(l.lines, "\n")
}
