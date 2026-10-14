package k

func Seq(yield func(int) bool) {
	for i := 0; i < 2; i++ {
		if !yield(i) {
			return
		}
	}
}

func a() {}
func b() {}

func Loop() {
	for x := range Seq {
		func() {
			defer a()
			defer func() { recover() }()
			if x == 1 {
				defer b()
				panic("p")
			}
		}()
	}
}
