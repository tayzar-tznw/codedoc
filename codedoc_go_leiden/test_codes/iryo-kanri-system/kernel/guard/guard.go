package guard

import "fmt"

type RequiredError struct {
	Name string
}

func (e RequiredError) Error() string {
	return e.Name + " は必須です"
}

func Require(v any, name string) error {
	if v == nil {
		return RequiredError{Name: name}
	}
	return nil
}

func EnsurePositive(n int) int {
	if n < 0 {
		panic(fmt.Sprintf("負の値: %d", n))
	}
	return n
}
