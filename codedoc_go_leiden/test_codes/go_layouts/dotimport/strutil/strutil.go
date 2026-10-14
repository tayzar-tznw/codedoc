package strutil

func Reverse(s string) string {
	r := []rune(s)
	for i, j := 0, len(r)-1; i < j; i, j = i+1, j-1 {
		swap(r, i, j)
	}
	return string(r)
}

func swap(r []rune, i, j int) {
	r[i], r[j] = r[j], r[i]
}
