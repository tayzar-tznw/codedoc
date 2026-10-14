package calc

func Triple(n int) int {
	return add(n, add(n, n))
}

func add(a, b int) int {
	return a + b
}
