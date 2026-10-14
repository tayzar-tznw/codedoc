package store

import "sort"

type MemoryStore[T any] struct {
	items map[int]T
}

func NewMemoryStore[T any]() *MemoryStore[T] {
	return &MemoryStore[T]{items: map[int]T{}}
}

func (s *MemoryStore[T]) Put(id int, v T) {
	s.items[id] = v
}

func (s *MemoryStore[T]) Get(id int) (T, bool) {
	v, ok := s.items[id]
	return v, ok
}

func (s *MemoryStore[T]) Count() int {
	return len(s.items)
}

func (s *MemoryStore[T]) IDs() []int {
	ids := make([]int, 0, len(s.items))
	for id := range s.items {
		ids = append(ids, id)
	}
	sort.Ints(ids)
	return ids
}

type Store[T any] = MemoryStore[T]

type Number interface {
	~int | ~int64 | ~float64
}

func FirstOf[T any](xs []T) T {
	var zero T
	if len(xs) == 0 {
		return zero
	}
	return xs[0]
}

func Sum[N Number](xs []N) N {
	var total N
	for _, x := range xs {
		total += x
	}
	return total
}

func Map[T, U any](xs []T, f func(T) U) []U {
	out := make([]U, 0, len(xs))
	for _, x := range xs {
		out = append(out, f(x))
	}
	return out
}
