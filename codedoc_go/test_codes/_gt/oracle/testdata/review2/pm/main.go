package main

func a() {}
func b() {}

func Two(fail bool) {
	defer a()
	if fail {
		defer b()
		defer func() { recover() }()
		panic("x")
	}
}

func main() { Two(false); Two(true) }
