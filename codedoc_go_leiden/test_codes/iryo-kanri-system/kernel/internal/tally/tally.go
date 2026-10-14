package tally

var totals = map[string]int{}

func bump(counter string, n int) int {
	totals[counter] += n
	return totals[counter]
}
