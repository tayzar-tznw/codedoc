package iface

import "example.com/lib/trace"

type Zero interface{ Nothing() }

type Single interface{ Only() string }

type OnlyImpl struct{}

func (OnlyImpl) Only() string {
	trace.Hit()
	return "OnlyImpl"
}

type Multi interface{ Do() string }

type A struct{}

func (A) Do() string {
	trace.Hit()
	return "A"
}

type B struct{}

func (*B) Do() string {
	trace.Hit()
	return "B"
}

type Logger interface {
	Printf(format string, v ...any)
}

type MyLog struct{}

func (MyLog) Printf(format string, v ...any) {
	trace.Hit()
}

type Sealed interface {
	sealed()
	Name() string
}

type S1 struct{}

func (S1) sealed() {}

func (S1) Name() string {
	trace.Hit()
	return "S1"
}

type S2 struct{}

func (S2) sealed() {}

func (S2) Name() string {
	trace.Hit()
	return "S2"
}

type Buf struct{ data []byte }

func (b *Buf) Write(p []byte) (int, error) {
	trace.Hit()
	b.data = append(b.data, p...)
	return len(p), nil
}
