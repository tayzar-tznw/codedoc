package textkit

import "strings"

func Banner(s string) string {
	return "** " + strings.ToLower(s) + " **"
}
