package w

func a()      {}
func c(n int) {}
func d()      {}

func G() {
	defer c(1)
	panic("g")
}

func F() {
	defer a()
	defer func() { recover() }()
	G()
}

func H() {
	defer d()
	panic("h")
}

func E() {
	defer a()
	defer func() { recover() }()
	H()
}
