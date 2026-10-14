package lined

//line gram.y:100
func Lined() string { return T{}.Name() }

//line m.go:10
func Lined2() string { return Lined() }

func CallLined() string { return Lined2() }
