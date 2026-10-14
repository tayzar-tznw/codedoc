package p

type inner struct{}

func (inner) Hook() string { return "inner" }

type Outer struct{ inner }

func Use(o Outer) string { return o.Hook() + Plain() }

func Plain() string { return "plain" }
