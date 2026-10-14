//go:build ignore

package lib

func Excluded() int { return Double(21) }

func init() { _ = Excluded() }
