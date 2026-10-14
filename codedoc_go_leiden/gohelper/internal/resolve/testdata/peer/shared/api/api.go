package api

type Handler interface{ Serve() string }

type Base struct{}

func (Base) Hello() string { return "base" }

type PeerImpl struct{}

func (PeerImpl) Serve() string { return "peer" }
