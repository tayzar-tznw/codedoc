package contracts

type Audit struct {
	By string
}

func (a Audit) Stamp() string {
	return "by " + a.By
}
