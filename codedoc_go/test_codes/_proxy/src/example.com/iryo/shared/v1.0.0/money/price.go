package money

type Price struct {
	yen int64
}

func NewPrice(yen int64) Price {
	return Price{yen: yen}
}

func (p Price) Add(o Price) Price {
	return Price{yen: p.yen + o.yen}
}

func (p Price) Amount() int64 {
	return p.yen
}
