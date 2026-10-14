package q

import "example.com/lost/api"

func Run(h api.Handler) error { return h.Handle() }

func Shut(c api.Closer) error { return c.Close(api.Conn{}) }
