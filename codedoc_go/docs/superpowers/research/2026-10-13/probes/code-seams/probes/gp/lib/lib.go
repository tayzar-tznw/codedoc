package lib

type Store interface {
	Get(k string) string
}

type mem struct{ m map[string]string }

func (s *mem) Get(k string) string { return s.m[k] }

func New() Store { return &mem{m: map[string]string{}} }

type Box[T any] struct{ v T }

func (b Box[T]) Val() T { return b.v }

func (b *Box[T]) Map[U any](f func(T) U) U { return f(b.v) }
