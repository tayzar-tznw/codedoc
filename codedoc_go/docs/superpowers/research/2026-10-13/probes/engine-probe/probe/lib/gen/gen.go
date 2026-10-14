package gen

import (
	"iter"

	"example.com/lib/trace"
)

func Map[T, U any](xs []T, f func(T) U) []U {
	trace.Hit()
	out := make([]U, 0, len(xs))
	for _, x := range xs {
		out = append(out, f(x)) //@GE16
	}
	return out
}

func Identity[T any](x T) T {
	trace.Hit()
	return x
}

type List[T any] struct{ items []T }

func (l *List[T]) Push(x T) {
	trace.Hit()
	l.items = append(l.items, x)
}

func (l *List[T]) Len() int {
	trace.Hit()
	return len(l.items)
}

type Stringer interface{ String() string }

func Describe[T Stringer](v T) string {
	trace.Hit()
	return v.String() //@GE13
}

type Named struct{ N string }

func (n Named) String() string {
	trace.Hit()
	return n.N
}

type Other struct{}

func (Other) String() string {
	trace.Hit()
	return "other"
}

type Alias[T any] = List[T]

func Count(n int) iter.Seq[int] {
	trace.Hit()
	return func(yield func(int) bool) {
		for i := 0; i < n; i++ {
			if !yield(i) { //@GE14
				return
			}
		}
	}
}

type Bag struct{ xs []int }

func (b *Bag) All(yield func(int) bool) {
	trace.Hit()
	for _, x := range b.xs {
		if !yield(x) { //@GE15
			return
		}
	}
}

type Box[E any] struct{ V E }

func (b Box[E]) Apply[F any](f func(E) F) F {
	trace.Hit()
	return f(b.V) //@GE17
}

type Num interface{ ~int | ~float64 }

func Sum[T Num](xs ...T) T {
	trace.Hit()
	var s T
	for _, x := range xs {
		s += x
	}
	return s
}

type Wrapped[T any] struct{ List[T] }
