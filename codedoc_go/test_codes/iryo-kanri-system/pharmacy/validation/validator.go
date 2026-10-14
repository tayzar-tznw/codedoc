package validation

import "strings"

type Validator struct{}

func (Validator) Validate(rx string) bool {
	return strings.HasPrefix(rx, "Rp")
}
