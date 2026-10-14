package api

import "context"

type Label string

type Manager struct{}

type Handler interface {
	Handle(ctx context.Context) error
}

type Named interface {
	Handle(ctx context.Context) error
	Name() Label
}

type Ready interface {
	Get() (*Manager, error)
}

type V struct{}

func (V) Handle(ctx context.Context) error { return nil }

func (V) Name() Label { return "" }
