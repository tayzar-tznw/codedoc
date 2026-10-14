package facts

import (
	"io"

	"example.com/cls/static"
)

type Base struct{}

func (Base) Hello() string { return "" }

type Server struct {
	Base
	*static.Counter
	io.Writer
}

type ReadCloser interface {
	io.Reader
	Close() error
}

type Shape interface{ Area() float64 }

type Square struct{}

func (Square) Area() float64 { return 1 }

type Circle struct{}

func (*Circle) Area() float64 { return 2 }

type Empty interface{}

func Use(s Server) string { return s.Hello() }

type ErrBox struct{ error }

type IntBox struct{ int }

type AnyBox struct{ any }

type Lit interface {
	interface{ Close() error }
	N()
}

type Number interface{ ~int | ~float64 }

type Cmp interface {
	comparable
	M()
}

func UseLit(l Lit) error { return l.Close() }
