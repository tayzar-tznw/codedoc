package validation

import "strings"

type Validator struct{}

func (Validator) Validate(id string) bool {
	return strings.HasPrefix(id, "H") && len(id) == 9
}
