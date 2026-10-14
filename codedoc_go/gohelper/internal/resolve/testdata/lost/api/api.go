package api

type Handler interface {
	Handle() error
}

type Conn struct{}

type Closer interface {
	Close(c Conn) error
}
