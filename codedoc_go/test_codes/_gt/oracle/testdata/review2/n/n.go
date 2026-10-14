package n

type I interface{ M() int }

type A struct{}

//go:nosplit
func (A) M() int { return 1 }

func Use(i I) int {
	return i.M()
}
