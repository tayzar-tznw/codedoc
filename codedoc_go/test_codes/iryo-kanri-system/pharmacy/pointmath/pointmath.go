package pointmath

func ToYen(points int) int {
	return points*10 + points*8/10
}
