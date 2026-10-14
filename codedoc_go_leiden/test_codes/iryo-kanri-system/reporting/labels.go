package reporting

func LabelAll[X interface{ Label() string }](items []X) []string {
	out := make([]string, 0, len(items))
	for _, it := range items {
		out = append(out, it.Label())
	}
	return out
}
