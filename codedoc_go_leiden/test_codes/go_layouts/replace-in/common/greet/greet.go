package greet

func Hello(name string) string {
	return "hello " + polish(name)
}

func polish(s string) string {
	return "<" + s + ">"
}
