package newsyntax

type intType interface{ ~int | ~int64 }

type Rand struct{ seed int64 }

// Go 1.27: generic method
func (r *Rand) N[Int intType](n Int) Int { return n }

type Inner struct{ X int }

type Outer struct{ Inner }

// Go 1.26: self-referential constraint
type Adder[A Adder[A]] interface{ Add(A) A }

func use(r *Rand) *int {
	_ = r.N[int](5)
	_ = r.N(int64(5))
	_ = Outer{X: 1}       // Go 1.27: promoted field as key
	return new(int(42))   // Go 1.26: new(expr)
}
