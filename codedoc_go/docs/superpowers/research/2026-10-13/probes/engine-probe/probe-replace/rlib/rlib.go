package rlib

func Do() string { return "rlib.Do" }

type Svc struct{}

func (Svc) Run() string { return "Svc.Run" }
