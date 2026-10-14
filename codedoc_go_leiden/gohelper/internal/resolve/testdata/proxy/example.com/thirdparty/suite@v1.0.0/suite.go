package suite

type Suite struct{ t any }

func (s *Suite) SetT(t any) string { s.t = t; return "suite" }
