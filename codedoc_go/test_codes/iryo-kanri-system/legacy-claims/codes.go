package rezept

func normalizeCode(name string) string {
	if name == "" {
		return "不明"
	}
	return name
}

func CodeLabel(code string) string {
	return code + ":" + LookupCode(code)
}
