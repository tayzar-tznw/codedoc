package values

import "io"

func target() string { return "t" }

func other() string { return "o" }

type T struct{}

func (T) M() string { return "m" }

type I interface{ M() string }

var pkgFn = target

type holder struct{ fn func() string }

func Identity[X any](x X) X { return x }

func Calls(param func() string, h holder, i I, fp *func() string, x any) {
	local := target
	_ = local()
	mv := T{}.M
	_ = mv()
	lit := func() string { return "lit" }
	_ = lit()
	im := i.M
	_ = im()
	re := target
	re = other
	_ = re()
	addr := target
	_ = &addr
	_ = addr()
	cl := target
	func() { cl = other }()
	_ = cl()
	_ = pkgFn()
	_ = param()
	_ = h.fn()
	m := map[string]func() string{"a": target}
	_ = m["a"]()
	fs := []func() string{target}
	_ = fs[0]()
	var noinit func() string
	noinit = target
	_ = noinit()
	gi := Identity[int]
	_ = gi(1)
	me := T.M
	_ = me(T{})
	var rv func() string = target
	_ = rv()
	_ = (*fp)()
	_ = x.(func() string)()
	multi, other2 := target, other
	_ = multi()
	_ = other2()
}

func named() (res func() string) {
	res = target
	_ = res()
	return
}

type RS struct{ io.Reader }

func Embedded(rs RS) {
	rd := rs.Read
	_, _ = rd(nil)
	me2 := io.Reader.Read
	_, _ = me2(rs, nil)
}
