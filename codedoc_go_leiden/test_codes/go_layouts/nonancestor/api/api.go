package api

func Version() string {
	return label("in-repo")
}

func label(s string) string {
	return "[" + s + "]"
}
