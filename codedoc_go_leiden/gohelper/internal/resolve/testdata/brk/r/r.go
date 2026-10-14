package r

type Base struct{}

func (Base) M() string { return "base" }

type Wrapper struct{ Base }
