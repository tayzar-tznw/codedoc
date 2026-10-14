package iface

import (
	"fmt"
	"io"
	"log"
)

type Zero interface{ Nothing() }

type Single interface{ Only() }

type OnlyImpl struct{}

func (OnlyImpl) Only() {}

type Multi interface{ Do() string }

type A struct{}

func (A) Do() string { return "a" }

type B struct{}

func (*B) Do() string { return "b" }

type Logger interface {
	Printf(format string, v ...any)
}

type MyLog struct{}

func (MyLog) Printf(format string, v ...any) {}

type Greeter interface{ Greet() string }

type English struct{}

func (English) Greet() string { return "hello" }

type Wrapper struct{ Greeter }

type Clock interface{ Now() int64 }

type realClock struct{}

func (realClock) Now() int64 { return 1 }

type ReadCloser interface {
	io.Reader
	Close() error
}

type Flusher interface{ Flush() }

type Named struct{}

func (Named) String() string { return "named" }

func Describe[T fmt.Stringer](v T) string { return v.String() }

func Flush(f Flusher) { f.Flush() }

func Calls(z Zero, s Single, m Multi, l Logger, w Wrapper, c Clock, rc ReadCloser, err error) {
	z.Nothing()
	s.Only()
	_ = m.Do()
	l.Printf("x")
	_ = w.Greet()
	_ = c.Now()
	_, _ = rc.Read(nil)
	_ = err.Error()
	_ = Multi.Do(m)
	_ = log.Prefix()
}

func Errs[T error](v T) string { return v.Error() }
