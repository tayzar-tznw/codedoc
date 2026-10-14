package good

import "example.com/lost/api"

type G struct{}

func (G) Close(c api.Conn) error { return nil }
