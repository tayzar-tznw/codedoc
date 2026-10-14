package escalation

type rule struct{}

func (rule) Level() string {
	return "通常"
}

type Policy struct {
	rule
	Ward string
}

func Evaluate(p Policy) string {
	return p.Ward + ":" + p.Level()
}
