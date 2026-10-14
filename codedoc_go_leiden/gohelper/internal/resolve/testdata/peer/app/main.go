package main

import "example.com/shared/api"

type Local struct{ api.Base }

type Impl struct{}

func (Impl) Serve() string { return "impl" }

func Use(h api.Handler) string { return h.Serve() }

func main() { _ = Use(Impl{}) + Local{}.Hello() }
