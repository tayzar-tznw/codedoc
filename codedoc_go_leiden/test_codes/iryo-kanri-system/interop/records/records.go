package records

type Record interface {
	Kind() string
	sealed()
}

type Claim struct {
	No string
}

func (Claim) Kind() string {
	return "請求"
}

func (Claim) sealed() {}

type Refund struct {
	No string
}

func (Refund) Kind() string {
	return "返金"
}

func (Refund) sealed() {}

func KindOf(r Record) string {
	return r.Kind()
}
