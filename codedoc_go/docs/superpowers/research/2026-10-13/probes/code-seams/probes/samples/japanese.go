package jp

// 計算する
func 計算(値 int) int { return 補助(値) + 補助(値) }

func 補助(x int) int { s := "日本"; _ = s; return x }

func F() { s := "日本語"; _ = 計算(len(s)) }
