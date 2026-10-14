package hdr

import (
	"example.com/lostb/api
)

type S struct{}

func (S) Close(c api.Conn) error { return nil }
