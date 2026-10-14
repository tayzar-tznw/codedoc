package hidden

func secret() string { return "s" }

func Public() string { return secret() }

type T struct{}

func (T) Value() int { return 1 }

func (*T) Ptr() int { return 2 }
