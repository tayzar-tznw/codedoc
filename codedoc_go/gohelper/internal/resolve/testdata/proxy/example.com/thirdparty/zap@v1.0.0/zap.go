package zap

type Logger struct{}

func New() *Logger { return &Logger{} }

type Core interface{ Write(s string) }

var Levels = []int{1, 2}

func Seq(yield func(int) bool) { yield(1) }

type Sugar struct{}

func (Sugar) Info(msg string) {}
