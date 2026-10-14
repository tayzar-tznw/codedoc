package r

func a()           {}
func report(n int) {}
func compute() int { panic("compute") }

func F() {
	defer a()
	defer func() { recover() }()
	defer report(compute())
}
