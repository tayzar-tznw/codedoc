package money

import "fmt"

type Yen int

func (y Yen) Add(o Yen) Yen {
	return y + o
}

func (y Yen) String() string {
	return fmt.Sprintf("%d円", int(y))
}

type Points int

func (p Points) ToYen() Yen {
	return Yen(p * 10)
}
