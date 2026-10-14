package embed

import "example.com/lib/trace"

type Base struct{}

func (Base) Hello() string {
	trace.Hit()
	return "Base.Hello"
}

func (Base) Shadowed() string {
	trace.Hit()
	return "Base.Shadowed"
}

func (*Base) PtrM() {
	trace.Hit()
}

type Mid struct{ Base }

func (Mid) Shadowed() string {
	trace.Hit()
	return "Mid.Shadowed"
}

type Top struct{ Mid }

type PtrEmb struct{ *Base }

type One struct{ Base }

type Greeter interface{ Greet() string }

type WithIface struct{ Greeter }

type English struct{}

func (English) Greet() string {
	trace.Hit()
	return "hi"
}

type Reader interface{ Read() string }

type ReadCloser interface {
	Reader
	Close()
}

type file struct{}

func (file) Read() string {
	trace.Hit()
	return "r"
}

func (file) Close() {
	trace.Hit()
}

func NewRC() ReadCloser { return file{} }
