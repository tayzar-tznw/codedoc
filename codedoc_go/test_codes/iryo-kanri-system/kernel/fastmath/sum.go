package fastmath

func Total(points []int64) int64 {
	var total int64
	for _, p := range points {
		total = AddPoints(total, p)
	}
	return total
}
