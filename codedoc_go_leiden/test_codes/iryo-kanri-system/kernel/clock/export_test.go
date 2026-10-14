package clock

func Normalize(label string) string {
	return normalize(label)
}

var NormalizeHook = normalize
