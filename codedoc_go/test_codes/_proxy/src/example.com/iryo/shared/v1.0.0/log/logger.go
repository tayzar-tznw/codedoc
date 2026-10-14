package log

import "fmt"

type Logger struct {
	name string
}

func New(name string) *Logger {
	return &Logger{name: name}
}

func (l *Logger) Info(msg string) {
	fmt.Println("[" + l.name + "] " + msg)
}
