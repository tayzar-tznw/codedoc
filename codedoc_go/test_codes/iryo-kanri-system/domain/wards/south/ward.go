package south

type Ward struct {
	Name string
}

func (w Ward) Beds() int {
	return 8
}
