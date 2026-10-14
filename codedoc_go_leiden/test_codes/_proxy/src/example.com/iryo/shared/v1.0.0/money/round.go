package money

func Round(yen int64) int64 {
	return (yen + 5) / 10 * 10
}
