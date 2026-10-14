package localiface

type I interface{ M() }

type A struct{}

func (A) M() {}

type B struct{}

func (B) M() {}

func (B) N() {}

func F(i I) { i.M() }

func G(b B) {
	type I interface {
		M()
		N()
	}
	var i I = b
	i.M()
}

func H(x any) {
	x.(interface{ N() }).N()
}

func K[T interface{ N() }](v T) { v.N() }
