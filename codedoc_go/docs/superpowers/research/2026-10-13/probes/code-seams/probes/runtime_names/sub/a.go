package sub

import "runtime"

func here() string {
	pc, _, _, _ := runtime.Caller(1)
	return runtime.FuncForPC(pc).Name()
}

var Names []string

func init() { Names = append(Names, here()) }
func init() { Names = append(Names, here()) }

var Hook = func() string { return here() }
var Hook2 = func() string { return func() string { return here() }() }

type T struct{}

func (T) Val() string  { return here() }
func (*T) Ptr() string { return here() }

type G[K comparable] struct{}

func (G[K]) Val() string  { return here() }
func (*G[K]) Ptr() string { return here() }

func (*T) Gen[X any](x X) string { return here() }

func F() string { return here() }

func Clos() []string {
	a := func() string { return here() }
	b := func() string {
		c := func() string { return here() }
		return c()
	}
	return []string{a(), b()}
}

func (*T) MClos() string { return func() string { return here() }() }

func Gen[X any](x X) string { return here() }

func GenClos[X any](x X) string { return func() string { return here() }() }
