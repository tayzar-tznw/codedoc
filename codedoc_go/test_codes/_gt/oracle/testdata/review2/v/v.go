package v

type Closer interface{ Close() }

type A struct{}

func (A) Close() {}

type Inner struct{}

func (Inner) Close() {}

type Outer struct{ Inner }

func compute() int { return 1 }
func cleanup()     {}

func ViaItab(c Closer) {
	defer c.Close()
	defer func() { recover() }()
	panic(compute())
}

func MethodValue(a A) {
	f := a.Close
	defer f()
	defer func() { recover() }()
	panic(compute())
}

func Promoted(c Closer) {
	defer cleanup()
	defer c.Close()
	defer func() { recover() }()
	panic(compute())
}
