package cache

type ChartCache[K comparable, V any] struct {
	data map[K]V
}

func New[K comparable, V any]() *ChartCache[K, V] {
	return &ChartCache[K, V]{data: map[K]V{}}
}

func (c *ChartCache[K, V]) Put(k K, v V) {
	c.data[k] = v
}

func (c *ChartCache[K, V]) Get(k K) (V, bool) {
	v, ok := c.data[k]
	return v, ok
}

func (c *ChartCache[K, V]) Len() int {
	return len(c.data)
}
