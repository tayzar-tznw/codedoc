package user2

type SetterT interface{ SetT(x any) string }

func Call(s SetterT) string { return s.SetT(nil) }
