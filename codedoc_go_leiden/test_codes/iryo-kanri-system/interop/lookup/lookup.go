package lookup

type Code string

type Source interface {
	Get() (Code, error)
}

type Fixed struct {
	Value Code
}

func (f Fixed) Get() (Code, error) {
	return f.Value, nil
}

func Resolve(s Source) string {
	code, err := s.Get()
	if err != nil {
		return "?"
	}
	return "コード " + string(code)
}
