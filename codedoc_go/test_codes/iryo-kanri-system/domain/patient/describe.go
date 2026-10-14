package patient

type Describer interface {
	Describe() string
}

func DescribeAll[T Describer](items []T) []string {
	out := make([]string, 0, len(items))
	for _, it := range items {
		out = append(out, it.Describe())
	}
	return out
}
