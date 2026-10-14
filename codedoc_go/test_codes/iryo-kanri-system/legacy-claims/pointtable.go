package rezept

type PointTable struct{}

func (PointTable) Calculate(points, count int) int {
	return points*count + 1000
}
