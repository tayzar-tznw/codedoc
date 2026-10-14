package policy

type PaymentPolicy interface {
	Fee(amount int) int
}

type BasePolicy struct {
	Rate int
}

func (b BasePolicy) Fee(amount int) int {
	return amount * b.Rate / 100
}

func (b BasePolicy) Label() string {
	return "標準"
}

type StandardPolicy struct {
	BasePolicy
}

type UrgentPolicy struct {
	BasePolicy
}

func (u UrgentPolicy) Fee(amount int) int {
	return amount * 25 / 100
}
