package registry

import "strings"

func init() {
	Register("trim", strings.TrimSpace)
}
