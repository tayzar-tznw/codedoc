package audit

import "fmt"

type Logger interface {
	Printf(format string, v ...any)
}

type ConsoleLogger struct {
	Prefix string
	Lines  []string
}

func (c *ConsoleLogger) Printf(format string, v ...any) {
	c.Lines = append(c.Lines, c.Prefix+fmt.Sprintf(format, v...))
}

func Record(l Logger, event string, count int) {
	l.Printf("監査 %s 件数=%d", event, count)
}
